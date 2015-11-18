#!/usr/bin/python

""" Command-line tool providing a loose integration between Smartling and Zendesk. 

Two basic functions are provided:

    1. Transfer from Zendesk to Smartling for translation. Transfers zendesk articles, 
    sections and categories from Zendesk to Smartling for translation.

    2. Transfer from Smartling to Zendesk. Transfers the translated versions of items 
    transferred above from Smartling to Zendesk, resulting in the creation or updating 
    of the 'translation' entity corresponding to the original source-language item 
    transferred to Smartling.

Special functionality is provided to fix up image src links and anchor href links in the
translated version so that they point to the translated version of the image or page.

Required configuration parameters are included in the file smartlingzd.cfg. Optional 
configuration parameters, used to control what items are transferred, are in the file 
translate.cfg.

The tool depends on two 'API' modules: zdesk and the Smartling Python SDK. The latter can 
be included in the current directory as that is added to the library path in the script.

No performance tuning has been done on the code.

Usage examples:

    ./smartlingzd.py --translate --articles 901922090,901922091 --categories all 
                     --loglevel debug
    ./smartlingzd.py -t -a 901922090,901922091 -c all -g debug

    Sends two articles and all categories from Zendesk to Smartling, with detailed logging

    ./smartlingzd.py --retrievetranslations --articles 901922090 --retrievaltype published
    ./smartlingzd.py -r -a 901922090 -y published 

    Tranfer the published translation for the specified article from Smartling to Zendesk

    ./smartlingzd -h
    
    Help on command-line options can be obtained by running with the '-h' option:

"""


import os
import sys
import io
import json
import re
import lxml.html
import argparse
import logging
import shutil
from urlparse import urlsplit
from ConfigParser import SafeConfigParser, Error

from zdesk import zdesk

lib_path = os.path.abspath('../')
sys.path.append(lib_path)  # allow to import ../smartlingApiSdk/SmartlingFileApi
from smartlingApiSdk.SmartlingFileApi import SmartlingFileApiFactory
from smartlingApiSdk.SmartlingDirective import SmartlingDirective
from smartlingApiSdk.UploadData import UploadData


__author__ = "Conall O'Raghallaigh"
__email__ = "conall@smartling.com"


CONFIG_FILE = 'smartlingzd.cfg'
TRANSFER_CONFIG_FILE = 'translate.cfg'

locale_mapping = {}   # Loaded from config file

# Source locale is hard-coded for now since misconfiguring could cause issues that 
# haven't been thought through or accounted for.
ZD_SOURCE_LOCALE = 'en-us' 

# Hard-code for now. Perhaps should be in config file.
SL_INCLUDE_ORIGINAL_STRINGS = True

# The items in Zendesk to be translated
TYPE_CATEGORY = 'category'
TYPE_SECTION = 'section'
TYPE_ARTICLE = 'article'

# Supported logging levels
LOGGING_LEVELS = {
    'info' : logging.INFO,
    'warning' : logging.WARNING,
    'error' : logging.ERROR,
    'critical' : logging.CRITICAL,
    'debug' : logging.DEBUG
}


# Internal use directories for storing JSON files representing the source and translated
# Zendesk items, which might be useful for debugging. Note that the files found 
# in TRANSLATION_DIR are translated versions of the source json files, translated 
# according to Smartling directives. The translation items loaded into Zendesk 
# are derived from these, but don't contain all the same values.
SOURCE_DIR = 'sourcefromzd'
TRANSLATION_DIR = 'translationsfromsl'


class SmartlingError(Exception):
    def __init__(self, msg, code, response):
        self.msg = msg
        self.error_code = code
        self.response = response

    def __str__(self):
        return repr('%s: %s %s' % (self.error_code, self.msg, self.response))


def fix_image_link(element, url, locale, article_attachments):
    """ Modifies image URL to point to the localised version.

    Given an image URL contains a substring indicating that it should be
    localised, then replace that substring its localised counterpart.

    The change is made directly in the passed-in HTML element rather than
    returning a copy.

    Because Zendesk assigns a new ID to each uploaded image, it's necessary
    to find the correct ID to include in the URL. This is done by searching
    through the article attachments for one that matches the name of the 
    localised image, and then using the URL of that attachment. 
    If the localised version is not found the link is left unchanged.
    """

    image_file_name = urlsplit(url).path.split('/')[-1]

    # Derive the localised image name. For example, change 'a_en-us.jpg' to 'a_fr.jpg'    
    # TODO externalise the '_en-us' bit
    new_image_file_name = re.sub(r'_en-us\.(...)',           
                                 '_' + locale + r'.\1',  
                                 image_file_name)

    # Search for the localised image, and use it's URL.
    found = False
    for attachment in article_attachments:
        if attachment['file_name'] == new_image_file_name:
            element.set('src', attachment['content_url'])
            found = True

    if not found:
        logging.warn('No %s version of image found for %s', locale, image_file_name)


def fix_anchor_link(element, url, locale):
    """ Modify anchor URL to point to translated version of page.

    Given an anchor URL replace a path component which contains the source
    locale with the localised equivalent. For example, substitues occurrence 
    of '/en-us/' with '/fr/'

    The change is made directly in the passed in HTML element rather than 
    returning a copy.
    """

    # TODO only do it for help-center links
    element.set('href', re.sub('/' + ZD_SOURCE_LOCALE + '/', 
                               '/' + locale + '/',            
                               url))


def fix_article_links(article_id, body, locale, zdapi):
    """ Replace links in article body with localised versions.

    Returns:
        Modified body string
    """

    logging.debug('Fixing links for article %s', article_id)

    fixed_body = body

    # Article attachment list is needed for localising the image links.
    try:
        attachments = zdapi.help_center_article_attachments(article_id)['article_attachments']  

    except zdesk.ZendeskError as e: 

        if e.error_code == 404:
            # uncommon situation - original article no longer exists so do nothing
            logging.info('Source article %s gone, skip link fixing...', article_id)

        else:
            raise

    else:
        # lxml will add a surrounding div if one isn't present, so check to see if it
        # should be removed at the end

        surrounding_div_in_orig = False
        if body.strip().lower().startswith('<div'):
            surrounding_div_in_orig = True

        # Parse the HTML and process each link
        parsed_body = lxml.html.fromstring(body)
        for element, attribute, link, pos in parsed_body.iterlinks():

            if element.tag == 'img':

                fix_image_link(element, link, locale, attachments)

            elif element.tag == 'a':

                fix_anchor_link(element, link, locale)

            else:
                logging.debug('Ignoring link element %s', element.tag)

        fixed_body = lxml.html.tostring(parsed_body)

        # Remove the surround div that lxml added
        if not surrounding_div_in_orig:
            # TODO is there an lxml way to do this
            if body.startswith('<div>') and body.endswith('</div>'):
                fixed_body = fixed_body[5:-6]


    return fixed_body


def upload_article_translation_to_zendesk(article_id, zd_locale, translation, zdapi):
    """ Upload an article translation to Zendesk. """

    logging.debug('Uploading article translation %s to Zendesk', article_id)

    try:
        # Assume it exists and just needs to be updated
        zdapi.help_center_article_translation_show(article_id, zd_locale)
        zdapi.help_center_article_translation_update(article_id, zd_locale, translation)

        logging.debug('Updated article translation %s translation, locale %s', 
                      str(article_id), zd_locale)

    except zdesk.ZendeskError as e: 

        if e.error_code == 404:
            # Translation doesnt exist, create it...
            try:
                logging.debug('Article translation not found. Creating...')
                zdapi.help_center_article_translation_create(article_id, translation)
                logging.debug('Added article translation %s, locale %s', 
                              article_id, zd_locale)

            except zdesk.ZendeskError as ee: 
                if ee.error_code == 404: 
                    # uncommon situation - source article no longer exisits
                    logging.debug('Source article %s gone, skip upload of translation...', 
                                  article_id)

                else:
                    raise

        else:
            raise


def upload_section_translation_to_zendesk(section_id, zd_locale, translation, zdapi):
    """ Uploads a section translation to Zendesk."""

    logging.debug('Uploading section translation %s to Zendesk', section_id)

    try:
        # Assume it exists and just needs to be updated
        zdapi.help_center_section_translation_show(section_id, zd_locale)
        zdapi.help_center_section_translation_update(section_id, zd_locale, translation)
        logging.debug('Updated section %s translation, locale %s', section_id, zd_locale)

    except zdesk.ZendeskError as e: 

        if e.error_code == 404:
            # doesnt exist, create it...
            try:
                zdapi.help_center_section_translation_create(section_id, translation)
                logging.debug('Added section %s translation, locale %s', 
                              section_id, zd_locale)

            except zdesk.ZendeskError as ee: 
                if ee.error_code == 404: # seems to give 404 if source item gone
                    # uncommon situation - source section no longer exisits
                    logging.debug('Source section %s gone, skip upload of translation...', 
                                  section_id)

                else:
                    raise

        else:
            raise


def upload_category_translation_to_zendesk(category_id, locale, translation, zdapi):
    """ Upload an category translation to Zendesk. """

    logging.debug('Uploading category translation %s to Zendesk', category_id)

    try:
        # Assume it exists and just needs to be updated
        zdapi.help_center_category_translation_show(category_id, locale)
        zdapi.help_center_category_translation_update(category_id, locale, translation)
        logging.debug('Updated category %s, locale %s', str(category_id), locale)

    except zdesk.ZendeskError as e: 

        if e.error_code == 404:
            # doesnt exist, create it...
            try:
                zdapi.help_center_category_translation_create(category_id, translation)
                logging.debug('Added category %s translation, locale %s', 
                              category_id, locale)

            except zdesk.ZendeskError as ee: 

                if ee.error_code == 404: # seems to give 404 if source item gone
                    # uncommon situation - source category no longer exisits
                    logging.debug('Source category %s gone, skip upload of translation...', 
                                  category_id)

                else:
                    raise

        else:
            raise


def construct_article_translation(item_id, translation_data, zd_locale, zdapi):
    """ Construct an object representing a Zendesk article translation.

    Since the source article JSON representation is uploaded to Smartling for 
    translation, the same JSON is returned with the specified fields translated. 
    However, there are some differences between a source article and its translation 
    in Zendesk. For example, the translation has a reference to the source locale. 
    In addition, we don't want to include the ID of the source article as the translation 
    will have its own ID. So we construct a simple object containing the translated
    fields and a couple of additional attribues.

    Arguments:
        item_id. Source article ID
        translation_data. The translation of the source article returned by Smartling
        zd_locale. The Zendesk locale of the translation
        zdapi. Reference to the Zendesk API

    Returns:
        Dictionary object with the fields needed by Zendesk to create/update an 
        article translation.
    """

    logging.debug('Constructing article translation, %s', item_id)

    translation = {}

    translation['locale'] = zd_locale
    translation['title'] = translation_data['title']
    translation['body'] = fix_article_links(item_id, 
                                            translation_data['body'], 
                                            zd_locale, zdapi)
    translation['draft'] = translation_data['draft']

    return translation


def construct_section_translation(translation_data, zd_locale):
    """ Construct an object representing a Zendesk section translation.

    See comments for construct_article_translation() above.

    Arguments:
        translation_data. The translation of the source article returned by Smartling

    Returns:
        Dictionary object with the fields needed by Zendesk to create/update an 
        section translation.
    """

    translation = {}

    translation['locale'] = zd_locale
    translation['name'] = translation_data['name']
    translation['description'] = translation_data['description']

    return translation


def construct_category_translation(translation_data, zd_locale):
    """ Construct an object representing a Zendesk category translation.

    See comments for construct_article_translation() above.

    Arguments:
        translation_data. The translation of the source article returned by Smartling

    Returns:
        Dictionary object with the fields needed by Zendesk to create/update an 
        section translation.
    """

    translation = {}

    translation['locale'] = zd_locale
    translation['name'] = translation_data['name']
    translation['description'] = translation_data['description']

    return translation


def write_to_file_json(item, full_file_name):
    """ Write a Dictionary to a file in JSON format """

    with io.open(full_file_name, 'w', encoding='utf-8') as f:
        f.write(unicode(json.dumps(item, sort_keys=True, indent=4, 
                                   ensure_ascii=False)))


def get_item_translation_file_name(item_type, item_id, locale):
    return item_type + '_' + str(item_id) + '_' + locale + '.json'


def write_item_translation_to_file(translation, item_type, locale):

    # The translation looks like the source item and not the translation item that
    # will eventually be uploaded to Zendesk. Therefore it doesn't have a 'source_id' key
    # yet. Since we need to tie it to the source item anyway, we just read the 'id' key 
    # since that will still contain the source value.
    item_id = translation['id'] 
    file_name = os.path.join(TRANSLATION_DIR, 
                             get_item_translation_file_name(item_type, item_id, locale))
    write_to_file_json(translation, file_name)


def download_translation_from_smartling_json(uri, sl_locale, retrieval_type, slapi):
    """ Download translation of a JSON file from Smartling

    Downloads the specified locale translation of a JSON file from Smartling.

    Arguments:
        uri. The URI of the original item in Smartling
        sl_locale. The language required
        retrieval_type. The Smartling type of translation to download (published, 
            pending or pseudo)
        slapi. Reference to the Smartling API

    Returns:
        Dictionary representing the parsed JSON downloaded from Smartling
    """

    logging.debug('Downloading from Smartling: %s', uri)

    response, http_response_code = slapi.get(fileUri=uri, 
                                             locale=sl_locale, 
                                             includeOriginalStrings=SL_INCLUDE_ORIGINAL_STRINGS,
                                             retrievalType=retrieval_type)

    if http_response_code == 200:

        return json.loads(response)

    else:
        # Might need to check for other response codes, but for now, give up

        raise SmartlingError('Error in Smartling API get call', 
                             http_response_code, response)



def transfer_translation_from_smartling(item_type, item_id, 
                                        sl_locale, retrieval_type, 
                                        slapi, zdapi):
    """ Transfer the translation or an item from Smartling to Zendesk.

    Article, section or category translation is downloaded from Smartling, 
    written to a file for logging purposes, then uploaded to Zendesk.
    """

    # The uri is the name of the source file that was uploaded to Smartling
    uri = get_source_item_file_name(item_type, item_id)

    logging.info('Transferring translation from Smartling: %s, locale %s', 
                 uri, sl_locale)

    # Download from Smartling
    translation_data = download_translation_from_smartling_json(uri, 
                                                           sl_locale, 
                                                           retrieval_type, 
                                                           slapi)

    if translation_data is None:
        logging.info('No translation with uri %s, locale, %s, retrivaltype, %s',
                     uri, sl_locale, retrieval_type)
        return

    # write it to a file in case useful for debugging
    write_item_translation_to_file(translation_data, item_type, sl_locale)

    # Upload to Zendesk
    zd_locale = get_zendesk_locale(sl_locale)
    if item_type == TYPE_ARTICLE:

        translation = construct_article_translation(item_id, translation_data, 
                                                    zd_locale, zdapi)
        upload_article_translation_to_zendesk(item_id, zd_locale, translation, zdapi)

    elif item_type == TYPE_SECTION:

        translation = construct_section_translation(translation_data)
        upload_section_translation_to_zendesk(item_id, zd_locale, translation, zdapi)

    elif item_type == TYPE_CATEGORY:

        translation = construct_category_translation(translation_data)
        upload_category_translation_to_zendesk(item_id, zd_locale, translation, zdapi)

    else:
        raise ValueError('Invalid item_type %r' % item_type)



def transfer_translations_from_smartling(item_type, item_ids, zd_locales, 
                                         retrieval_type, slapi, zdapi):
    """ Transfer a list of item_type translations from Smartling to Zendesk."""

    logging.info('Transferring %s translations from Smartling', item_type)

    for item_id in item_ids:
        for zd_locale in zd_locales:
            sl_locale = get_smartling_locale(zd_locale)
            transfer_translation_from_smartling(item_type, item_id, 
                                                sl_locale, retrieval_type, 
                                                slapi, zdapi)


def get_completed_ids(item_type, sl_locale, slapi):
    """ Return a list of IDs for which we have completed translations in Smartling. """

    response, http_response_code = slapi.list(locale=sl_locale,
                                              uriMask=item_type,
                                              conditions='haveAllTranslated',
                                              fileTypes='json')

    response_code = response.code
    response_messages = response.messages

    ids = []

    if http_response_code == 200:

        response_data = response.data

        file_count = response_data.fileCount

        # TODO don't re-run if only one page

        # Smartling list API returns items in 500-entry pages
        num_loops_required = (file_count / 500) + 1  

        for n in range(num_loops_required):

            response, http_response_code = slapi.list(locale=sl_locale, 
                                                      uriMask=item_type,
                                                      conditions='haveAllTranslated',
                                                      fileTypes='json',
                                                      offset = n * 500)

            response_code = response.code
            response_messages = response.messages

            if http_response_code == 200:

                response_data = response.data

                for file in response_data.fileList:

                    uri = file['fileUri']
                    id = uri.split('_')[1].split('.')[0]
                    ids.append(int(id))

            else:

                raise SmartlingError('Error in Smartling API upload call', 
                             http_response_code, response)

    else:

        raise SmartlingError('Error in Smartling API upload call', 
                             http_response_code, response)

    return ids


def transfer_all_translations_from_smartling(item_type, zd_locales, 
                                             include_articles, exclude_articles,
                                             retrieval_type, slapi, zdapi):
    """ Transfers all item_type translations from Smartling to Zendesk.

    Gets the full list of source items from Zendesk, then downloads the corresponding
    translations in the specified locales from Smartling and uploads to Zendesk.

    Note that there is a small race condition between the list and download during which
    a new source file could be uploaded for a particular ID resulting in a partially
    translated file being downloaded.
    """

    # Get a list of valid source items first, which will be used to filter
    # what we actually tranfer from Smartling

    filtered_source_ids = set()
    for item in get_all_source_items_from_zendesk(item_type, 
                                                  include_articles, 
                                                  exclude_articles,
                                                  zdapi):

        filtered_source_ids.add(item['id'])


    # For each locale, get the list of items that have been fully translated in Smartling.
    # Then for each of those, if it appears on the list of items for which we need translations, 
    # transfer it over.

    for zd_locale in zd_locales:
        sl_locale = get_smartling_locale(zd_locale)

        for completed_id in get_completed_ids(item_type, sl_locale, slapi):

            if completed_id in filtered_source_ids:

                transfer_translation_from_smartling(item_type, completed_id, 
                                                    sl_locale, retrieval_type, 
                                                    slapi, zdapi)


def get_source_item_file_name(item_type, item_id):
    return item_type + '_' + str(item_id) + '.json'


def write_item_to_file(item, item_type, directory):
    """ Writes a dictionary to a file in JSON format.

    Arguments:
        item. Dictionary to be written to file.
        item_type. Either article, category or section.
        directory. The directory where the file should be written.
    """

    item_id = item['id']
    file_name = os.path.join(directory, get_source_item_file_name(item_type, item_id))
    write_to_file_json(item, file_name)


def upload_source_file_to_smartling(path, file_name, file_type, 
                                    fields_to_translate, approve, slapi):
    """ Upload a source file of type file_type to Smartling. """

    path = path + '/' # sdk requires TODO check this

    upload_data = UploadData(path, file_name, file_type, file_name)

    upload_data.setUri(file_name) 

    if approve:
        upload_data.setApproveContent('true')
    else:
        upload_data.setApproveContent('false')

    
    upload_data.addDirective(SmartlingDirective('translate_paths', 
                                                ','.join(fields_to_translate)))
    upload_data.addDirective(SmartlingDirective('string_format_paths', 'html:body'))
    upload_data.addDirective(SmartlingDirective('source_key_paths', 'title'))
    upload_data.addDirective(SmartlingDirective('smartling.namespace', 'zendesk'))

    response, http_response_code = slapi.upload(upload_data)

    if http_response_code == 200:

        response_data = response.data

        logging.debug('Uploaded to Smartling: %s', file_name)
        logging.debug('Overwritten: %s, String count: %s, Word count: %s', 
                      response_data.overWritten, response_data.stringCount, 
                      response_data.wordCount)

    else:
        raise SmartlingError('Error in Smartling API upload call', 
                             http_response_code, response)


def get_fields_to_translate(item_type):

    if item_type == TYPE_ARTICLE:
        return ['body', 'title']

    elif item_type == TYPE_SECTION:
        return ['name', 'description']

    elif item_type == TYPE_CATEGORY:
        return ['name', 'description']

    else:
        raise ValueError('Invalid item_type %r' % item_type )


def upload_item_to_smartling(item, item_type, approve, slapi):
    """ Uploads an article, section or category object to Smartling.

    Writes to item to a JSON file, then uploads the file to Smartling.

    Arguments:
        item. Dictionary representing the item
        item_type. article, section or category
    """

    item_id = item['id']

    write_item_to_file(item, item_type, SOURCE_DIR)
    file_name = get_source_item_file_name(item_type, item_id)

    fields = get_fields_to_translate(item_type)
    file_format = 'json'

    logging.info('Uploading to Smartling: ' + file_name)
    upload_source_file_to_smartling(SOURCE_DIR, file_name, file_format, 
                                    fields, approve, slapi)



def transfer_source_item_to_smartling(item_type, item_id, approve, slapi, zdapi):
    """ Transfer an item from Zendesk to Smartling for translation.

    First, download the article, section or category from Zendesk, then upload
    to Smartling.
    """

    logging.info('Transferring %s %s from Zendesk to Smartling', item_type, item_id)

    item = None

    try:
        if item_type == TYPE_ARTICLE:

            item = zdapi.help_center_article_show(id=item_id)['article']

        elif item_type == TYPE_SECTION:    

            item = zdapi.help_center_section_show(id=item_id)['section']

        elif item_type == TYPE_CATEGORY:

            item = zdapi.help_center_category_show(id=item_id)['category']

        else:
            raise ValueError('Invalid item_type %r' % item_type )

    except zdesk.ZendeskError as e:

        if e.error_code == 404:
            logging.warn('%s not found. ID: %s', item_type, item_id)
        else:
            raise

    else:
        upload_item_to_smartling(item, item_type, approve, slapi)


def transfer_source_items_to_smartling(item_type, item_ids, approve, slapi, zdapi):
    """ Transfer a list of source items of one type from Zendesk to Smartling. """

    logging.info('Transferring %s items to Smartling for translation. IDs: %s', 
                    item_type, str(item_ids))

    for item_id in item_ids:
        transfer_source_item_to_smartling(item_type, item_id, approve, slapi, zdapi)        


def get_all_source_items_from_zendesk(item_type, include_articles, exclude_articles, zdapi):
    """ Return all source items in Zendesk, possibly filtered.

    Return all items of the specified type from Zendesk. In the case of articles, they returned
    list is filtered according to what is in include_articles and exclude_articles.
    Draft articles are not included, unless articles are specified in the include_articles 
    argument, in which case the draft attribute is ignored.
    """

    logging.info('Getting all %s items from Zendesk', item_type)

    items = []

    if item_type == TYPE_ARTICLE:

        full_list = zdapi.help_center_articles(ZD_SOURCE_LOCALE, get_all_pages=True)['articles']

        # Don't include 'draft' articles unless a specific list of articles to include has 
        # been given.
        if len(include_articles) == 0:
            include_draft = False
        else:
            include_draft = True

        for item in full_list:
            id = item['id']

            if include_draft is False and item['draft'] is True:
                logging.info('Skipping draft article %s', id)
                continue

            if len(include_articles) > 0:
                if (id in include_articles) and (id not in exclude_articles):
                    items.append(item)
                else:
                    logging.info('Skipping article %s due to transfer config', id)

            else:
                if id not in exclude_articles:
                    items.append(item)
                else:
                    logging.info('Skipping article %s due to transfer config', id)
       

    elif item_type == TYPE_SECTION:
    
        items = zdapi.help_center_sections(ZD_SOURCE_LOCALE, get_all_pages=True)['sections']

    elif item_type == TYPE_CATEGORY:

        items = zdapi.help_center_categories(ZD_SOURCE_LOCALE, get_all_pages=True)['categories']

    else:
        raise ValueError('Invalid item_type %r' % item_type)

    logging.info('Got %s items', len(items))
    return items


def transfer_all_source_items_to_smartling(item_type, include_articles, exclude_articles, 
                                           approve, slapi, zdapi):
    """ Transfer all source items of a particular type from Zendesk to Smartling. """

    logging.info('Transferring all %s items to Smartling for translation', item_type)


    for item in get_all_source_items_from_zendesk(item_type, 
                                                  include_articles, 
                                                  exclude_articles, 
                                                  zdapi):
        upload_item_to_smartling(item, item_type, approve, slapi)


def clean_dir(d):  
    if os.path.exists(d):
        shutil.rmtree(d, ignore_errors=True)    

    os.makedirs(d)


def get_smartling_locale(zd_locale):
    if zd_locale not in locale_mapping.keys():
        raise ValueError('Invalid Zendesk locale: %s' % zd_locale)
    return locale_mapping[zd_locale.lower()]


def get_zendesk_locale(sl_locale):
    for k, v in locale_mapping.iteritems():
        if v == sl_locale:
            return k

    raise ValueError('Invalid Zendesk locale: %s' % sl_locale)


def is_valid_locale_list(locales):
    if locales == 'all':
        return True

    locale_list = locales.split(',')

    for locale in locale_list:
        if locale not in locale_mapping.keys():
            return False

    return True


def main():

    # Load configuration parameters.

    config = SafeConfigParser()
    config.read(CONFIG_FILE)
    
    try:
        log_file = config.get('general', 'log_file')
        sl_api_key = config.get('smartling', 'api_key')        
        sl_project_id = config.get('smartling', 'project_id')
        if config.has_option('smartling', 'approve_for_translation'):
            approve_for_translation = config.getboolean('smartling', 'approve_for_translation')
        else:
            approve_for_translation = True


        zd_url = config.get('zendesk', 'url')
        zd_user = config.get('zendesk', 'user')
        zd_auth_token = config.get('zendesk', 'auth_token')
        
        for key, val in config.items('zd-to-sl-locales'):
            locale_mapping[key] = val
        
    except Error as e:
        sys.exit('Configuration error in ' + CONFIG_FILE + ': ' + str(e))
    
    # Load transfer config file

    transfer_config = SafeConfigParser(allow_no_value=True)
    transfer_config.read(TRANSFER_CONFIG_FILE)

    transfer_config_valid_sections = ['include-articles', 'exclude-articles']
    for section in transfer_config.sections():
        if section not in transfer_config_valid_sections:
            sys.exit('Invalid section in ' + TRANSFER_CONFIG_FILE + ': ' + section)

    include_articles = []
    if transfer_config.has_section('include-articles'):
        for option in transfer_config.options('include-articles'):
            try:
                include_articles.append(int(option))
            except ValueError as e:
                sys.exit('Invalid entry in ' + TRANSFER_CONFIG_FILE + ': ' + option)

    exclude_articles = []
    if transfer_config.has_section('exclude-articles'):
        for option in transfer_config.options('exclude-articles'):
            try:
                exclude_articles.append(int(option))
            except ValueError as e:
                sys.exit('Invalid entry in ' + TRANSFER_CONFIG_FILE + ': ' + option)



    # Parse command line.

    parser = argparse.ArgumentParser()

    parser.add_argument('-t', '--translate', 
                        action='store_true', 
                        dest='translate', 
                        default=False,
                        help='Get source files from Zendesk and send to Smartling for translation')

    parser.add_argument('-r', '--retrievetranslations', 
                        action='store_true', 
                        dest='retrieve', 
                        default=False, 
                        help='Retrieve translations from Smartling and upload to Zendesk')

    parser.add_argument('-l', '--locales', 
                        action='store', 
                        dest='locales',
                        help='Which locale translations to translate or retrieve, or all')

    parser.add_argument('-a', '--articles', 
                        action='store', 
                        dest='articles', 
                        help='Comma-separated list of article IDs to send or retrieve, or all')

    parser.add_argument('-s', '--sections',  
                        action='store', 
                        dest='sections', 
                        help='Comma-separated list of section IDs to send or retrieve, or all')

    parser.add_argument('-c', '--categories',  
                        action='store', 
                        dest='categories', 
                        help='Comma-separated list of category IDs to send or retrieve, or all')

    parser.add_argument('-y', '--retrievaltype', 
                        action='store',
                        dest='retrievaltype',
                        required=False, 
                        default='published',
                        help='Type of translation to retrieve - published, pseudo or pending')

    parser.add_argument('-g', '--loglevel', 
                        action='store',
                        dest='loglevel',
                        required=False, 
                        default='info', 
                        help='Logging level')

    args = parser.parse_args()

    if args.translate == False and args.retrieve == False:
        print 'Please specify either translate (-t) or retrieve (-r)'
        return

    if args.translate == True and args.retrieve == True:
        print 'Please specify either translate (-t) or retrieve (-r)'
        return

    if args.translate and args.locales:
        print 'Locales currently only supported for retrieval'
        return

    if args.retrieve and not args.locales:
        print 'Please specify locales, or all'
        return

    if args.locales:
        if is_valid_locale_list(args.locales):
            if args.locales == 'all':
                locales = locale_mapping.keys()
            else:
                locales = args.locales.split(',')
        else:
            print 'Please specify a valid comma-separated list of locales, or leave blank'
            print 'Valid locales: ' + ','.join(locale_mapping.keys())
            return

    if args.retrievaltype and args.retrievaltype not in ['published', 'pseudo', 'pending']:
        print 'Please specify a valid retrieval type (published, pseudo or pending), or leave blank'
        return

    if args.loglevel and args.loglevel not in LOGGING_LEVELS.keys():
        print 'Please specify a valid logging level (debug, info, warning, error, critical), or leave blank'
        return

    # Initialise logging.

    log_format = '%(asctime)-15s %(levelname)s: %(message)s'
    if args.loglevel:
        logging.basicConfig(filename=log_file, 
                            format=log_format,
                            level=LOGGING_LEVELS[args.loglevel])
    else:
        logging.basicConfig(filename=log_file, 
                            format=log_format, 
                            level=logging.INFO)


    zdapi = zdesk.Zendesk(zd_url, zd_user, zd_auth_token, True)
    slapi = SmartlingFileApiFactory().getSmartlingTranslationApiProd(sl_api_key, 
                                                                     sl_project_id)

    try:

        if args.translate:

            logging.info('----------------------------------------------')
            logging.info('Beginning transfer of source content to Smartling...')

            clean_dir(SOURCE_DIR)

            # TODO separate 'all articles' calls from categories and sections 
            # or make include/exclude optional

            if args.categories: 

                if args.categories == 'all':
                    transfer_all_source_items_to_smartling(TYPE_CATEGORY,
                                                           include_articles,
                                                           exclude_articles,
                                                           approve_for_translation,
                                                           slapi, zdapi)

                else:
                    item_ids = args.categories.split(',')
                    transfer_source_items_to_smartling(TYPE_CATEGORY, item_ids, 
                                                       approve_for_translation,
                                                       slapi, zdapi)

            if args.sections:   

                if args.sections == 'all':
                    transfer_all_source_items_to_smartling(TYPE_SECTION, 
                                                           include_articles,
                                                           exclude_articles,
                                                           approve_for_translation,
                                                           slapi, zdapi)

                else:
                    item_ids = args.sections.split(',')
                    transfer_source_items_to_smartling(TYPE_SECTION, 
                                                       item_ids, 
                                                       approve_for_translation,
                                                       slapi, zdapi)

            if args.articles:   

                if args.articles == 'all':
                    transfer_all_source_items_to_smartling(TYPE_ARTICLE, 
                                                           include_articles,
                                                           exclude_articles,
                                                           approve_for_translation,
                                                           slapi, zdapi)

                else:
                    item_ids = args.articles.split(',')
                    transfer_source_items_to_smartling(TYPE_ARTICLE, 
                                                       item_ids, 
                                                       approve_for_translation,
                                                       slapi, zdapi)


        elif args.retrieve:

            logging.info('-------------------------------------------------')
            logging.info('Beginning tranfer of translations from Smartling...')

            clean_dir(TRANSLATION_DIR)

            if args.categories:   

                if args.categories == 'all':
                    transfer_all_translations_from_smartling(
                        TYPE_CATEGORY, locales, 
                        include_articles,
                        exclude_articles,
                        'published', slapi, zdapi
                        )

                else:
                    item_ids = args.categories.split(',')
                    transfer_translations_from_smartling(
                        TYPE_CATEGORY, item_ids, locales, 
                        args.retrievaltype , slapi, zdapi
                        )

            if args.sections:   

                if args.sections == 'all':
                    transfer_all_translations_from_smartling(
                        TYPE_SECTION, locales, 
                        include_articles,
                        exclude_articles,
                        'published', slapi, zdapi
                        )

                else:
                    item_ids = args.sections.split(',')
                    transfer_translations_from_smartling(
                        TYPE_SECTION, item_ids, locales, 
                        args.retrievaltype , slapi, zdapi
                        )

            if args.articles:   

                if args.articles == 'all':
                    transfer_all_translations_from_smartling(
                        TYPE_ARTICLE, 
                        locales, 
                        include_articles,
                        exclude_articles,
                        'published', slapi, zdapi
                        )

                else:
                    item_ids = args.articles.split(',')
                    transfer_translations_from_smartling(
                        TYPE_ARTICLE, item_ids, locales, 
                        args.retrievaltype , slapi, zdapi
                        )


    except zdesk.ZendeskError as e:

        # Not handling API rate limit errors as they seem unlikely given we're single-threaded
        # also not handling authentication errors as they'll likely only be during setup
        # could be others that need handling

        logging.critical('Zendesk API error %s: %s', e.error_code, e.msg)
        logging.critical(e.response)
        sys.exit('Zendesk API error %s. Check log for details.' % e.error_code)

    except SmartlingError as e:

        logging.critical('Smartling API error %s: %s', e.error_code, e.msg)
        logging.critical(e.response)
        sys.exit('Smartling API error %s. Check log for details.' % e.error_code)


if __name__ == "__main__":
    main()


