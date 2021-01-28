# -*- coding: utf-8 -*-
#########################################################################
#
# Copyright (C) 2016 OSGeo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

"""
See the README.rst in this directory for details on running these tests.
@todo allow using a database other than `development.db` - for some reason, a
      test db is not created when running using normal settings
@todo when using database settings, a test database is used and this makes it
      difficult for cleanup to track the layers created between runs
@todo only test_time seems to work correctly with database backend test settings
"""

from geonode.tests.base import GeoNodeBaseTestSupport

import os.path
from django.conf import settings
from django.db import connections, transaction
from django.contrib.auth import get_user_model

from geonode.maps.models import Map
from geonode.layers.models import Layer
from geonode.upload.models import Upload
from geonode.documents.models import Document
from geonode.base.models import Link
from geonode.catalogue import get_catalogue
from geonode.tests.utils import upload_step, Client
from geonode.upload.utils import _ALLOW_TIME_STEP
from geonode.geoserver.helpers import ogc_server_settings, cascading_delete
from geonode.geoserver.signals import gs_catalog

from geoserver.catalog import Catalog
from gisdata import BAD_DATA
from gisdata import GOOD_DATA
from owslib.wms import WebMapService
from zipfile import ZipFile
from six import string_types

import re
import os
import csv
import glob
import time
from urllib.parse import unquote, urlsplit
from urllib.error import HTTPError
import logging
import tempfile
import unittest
import dj_database_url

GEONODE_USER = 'admin'
GEONODE_PASSWD = 'admin'
GEONODE_URL = settings.SITEURL.rstrip('/')
GEOSERVER_URL = ogc_server_settings.LOCATION
GEOSERVER_USER, GEOSERVER_PASSWD = ogc_server_settings.credentials

DB_HOST = settings.DATABASES['default']['HOST']
DB_PORT = settings.DATABASES['default']['PORT']
DB_NAME = settings.DATABASES['default']['NAME']
DB_USER = settings.DATABASES['default']['USER']
DB_PASSWORD = settings.DATABASES['default']['PASSWORD']
DATASTORE_URL = 'postgis://{}:{}@{}:{}/{}'.format(
    DB_USER,
    DB_PASSWORD,
    DB_HOST,
    DB_PORT,
    DB_NAME
)
postgis_db = dj_database_url.parse(DATASTORE_URL, conn_max_age=0)

logging.getLogger('south').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# create test user if needed, delete all layers and set password
u, created = get_user_model().objects.get_or_create(username=GEONODE_USER)
if created:
    u.first_name = "Jhònà"
    u.last_name = "çénü"
    u.set_password(GEONODE_PASSWD)
    u.save()
else:
    Layer.objects.filter(owner=u).delete()


def get_wms(version='1.1.1', type_name=None, username=None, password=None):
    """ Function to return an OWSLib WMS object """
    # right now owslib does not support auth for get caps
    # requests. Either we should roll our own or fix owslib
    if type_name:
        url = GEOSERVER_URL + \
            '%swms?request=getcapabilities' % type_name.replace(':', '/')
    else:
        url = GEOSERVER_URL + \
            'wms?request=getcapabilities'
    ogc_server_settings = settings.OGC_SERVER['default']
    if username and password:
        return WebMapService(
            url,
            version=version,
            username=username,
            password=password,
            timeout=ogc_server_settings.get('TIMEOUT', 60)
        )
    else:
        return WebMapService(url, timeout=ogc_server_settings.get('TIMEOUT', 60))


class UploaderBase(GeoNodeBaseTestSupport):

    type = 'layer'

    @classmethod
    def setUpClass(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        if os.path.exists('integration_settings.py'):
            os.unlink('integration_settings.py')

    def setUp(self):
        # await startup
        cl = Client(
            GEONODE_URL, GEONODE_USER, GEONODE_PASSWD
        )
        for i in range(10):
            time.sleep(.2)
            try:
                cl.get_html('/', debug=False)
                break
            except Exception:
                pass

        self.client = Client(
            GEONODE_URL, GEONODE_USER, GEONODE_PASSWD
        )
        self.catalog = Catalog(
            GEOSERVER_URL + 'rest',
            GEOSERVER_USER,
            GEOSERVER_PASSWD,
            retries=ogc_server_settings.MAX_RETRIES,
            backoff_factor=ogc_server_settings.BACKOFF_FACTOR
        )

        settings.DATABASES['default']['NAME'] = DB_NAME

        connections['default'].settings_dict['ATOMIC_REQUESTS'] = False
        connections['default'].connect()

        self._tempfiles = []

    def _post_teardown(self):
        pass

    def tearDown(self):
        connections.databases['default']['ATOMIC_REQUESTS'] = False

        for temp_file in self._tempfiles:
            os.unlink(temp_file)

        # Cleanup
        try:
            with transaction.atomic():
                Upload.objects.all().delete()
                Layer.objects.all().delete()
                Map.objects.all().delete()
                Document.objects.all().delete()
        except Exception as e:
            logger.error(e)

        if settings.OGC_SERVER['default'].get(
                "GEOFENCE_SECURITY_ENABLED", False):
            from geonode.security.utils import purge_geofence_all
            purge_geofence_all()



    def upload_file(self, fname, final_check,
                    check_name=None, session_ids=None):
        if not check_name:
            check_name, _ = os.path.splitext(fname)
        resp, data = self.client.upload_file(fname)
        if session_ids is not None:
            if not isinstance(data, string_types):
                if data.get('url'):
                    session_id = re.search(
                        r'.*id=(\d+)', data.get('url')).group(1)
                    if session_id:
                        session_ids += [session_id]
        if not isinstance(data, string_types):
            self.wait_for_progress(data.get('progress'))
        final_check(check_name, resp, data)

    def wait_for_progress(self, progress_url):
        if progress_url:
            resp = self.client.get(progress_url)
            print("progress_url: " + str(progress_url))
            logger.warning("progress_url: " + str(progress_url))
            print("Resp: " + str(resp))
            logger.warning("Resp: " + str(resp))
            json_data = resp.json()
            print("json_data: " + str(json_data))
            # "COMPLETE" state means done
            if json_data.get('state', '') == 'RUNNING':
                print("running inside loop " + str(json_data.get('state', '')))
                logger.warning("running " + str(json_data.get('state', '')))
                time.sleep(0.1)
                self.wait_for_progress(progress_url)

    def temp_file(self, ext):
        fd, abspath = tempfile.mkstemp(ext)
        self._tempfiles.append(abspath)
        return fd, abspath

    def make_csv(self, fieldnames, *rows):
        fd, abspath = self.temp_file('.csv')
        with open(abspath, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        return abspath


class TestUpload(UploaderBase):


    def test_coherent_importer_session(self):
        """ Tests that the upload computes correctly next session IDs"""
        session_ids = []
"""
        # First of all lets upload a raster
        fname = os.path.join(GOOD_DATA, 'raster', 'relief_san_andres.tif')
        self.assertTrue(os.path.isfile(fname))

        self.upload_file(
            fname,
            self.complete_raster_upload,
            session_ids=session_ids)

        # Next force an invalid session
        invalid_path = os.path.join(BAD_DATA)
        self.upload_folder_of_files(
            invalid_path,
            self.check_invalid_projection,
            session_ids=session_ids)

        # Finally try to upload a good file anc check the session IDs
        fname = os.path.join(GOOD_DATA, 'raster', 'relief_san_andres.tif')
        self.upload_file(
            fname,
            self.complete_raster_upload,
            session_ids=session_ids)

        self.assertTrue(len(session_ids) >= 0)
        if len(session_ids) > 1:
            self.assertTrue(int(session_ids[0]) < int(session_ids[1]))
"""
    def test_extension_not_implemented(self):
        """Verify a error message is return when an unsupported layer is
        uploaded"""

        # try to upload ourselves
        # a python file is unsupported
        unsupported_path = __file__
        if unsupported_path.endswith('.pyc'):
            unsupported_path = unsupported_path.rstrip('c')

        with self.assertRaises(HTTPError):
            self.client.upload_file(unsupported_path)

    def test_csv(self):
        '''make sure a csv upload fails gracefully/normally when not activated'''
        csv_file = self.make_csv(
            ['lat', 'lon', 'thing'], {'lat': -100, 'lon': -40, 'thing': 'foo'})
        layer_name, ext = os.path.splitext(os.path.basename(csv_file))
        resp, data = self.client.upload_file(csv_file)
        self.assertEqual(resp.status_code, 200)
        if not isinstance(data, string_types):
            self.assertTrue('success' in data)
            self.assertTrue(data['success'])
            self.assertTrue(data['redirect_to'], "/upload/csv")


@unittest.skipUnless(ogc_server_settings.datastore_db,
                     'Vector datastore not enabled')
class TestUploadDBDataStore(UploaderBase):

    def test_csv(self):
        """Override the baseclass test and verify a correct CSV upload"""

        csv_file = self.make_csv(
            ['lat', 'lon', 'thing'], {'lat': -100, 'lon': -40, 'thing': 'foo'})
        layer_name, ext = os.path.splitext(os.path.basename(csv_file))
        resp, form_data = self.client.upload_file(csv_file)
        self.assertEqual(resp.status_code, 200)
        if not isinstance(form_data, string_types):
            self.check_save_step(resp, form_data)
            csv_step = form_data['redirect_to']
            self.assertTrue(upload_step('csv') in csv_step)
            form_data = dict(
                lat='lat',
                lng='lon',
                csrfmiddlewaretoken=self.client.get_csrf_token())
            resp = self.client.make_request(csv_step, form_data)
            content = resp.json()
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(content['status'], 'incomplete')

    def test_time(self):
        """Verify that uploading time based shapefile works properly"""
        cascading_delete(layer_name='boxes_with_date', catalog=self.catalog)

        timedir = os.path.join(GOOD_DATA, 'time')
        layer_name = 'boxes_with_date'
        shp = os.path.join(timedir, '%s.shp' % layer_name)

        # get to time step
        resp, data = self.client.upload_file(shp)
        self.assertEqual(resp.status_code, 200)
        if not isinstance(data, string_types):
            self.wait_for_progress(data.get('progress'))
            self.assertTrue(data['success'])
            self.assertTrue(data['redirect_to'], upload_step('time'))
            redirect_to = data['redirect_to']
            resp, data = self.client.get_html(upload_step('time'))
            self.assertEqual(resp.status_code, 200)
            data = dict(csrfmiddlewaretoken=self.client.get_csrf_token(),
                        time_attribute='date',
                        presentation_strategy='LIST',
                        )
            resp = self.client.make_request(redirect_to, data)
            self.assertEqual(resp.status_code, 200)
            resp_js = resp.json()
            if resp_js['success']:
                url = resp_js['redirect_to']

                resp = self.client.make_request(url, data)

                url = resp.json()['url']

                self.assertTrue(
                    url.endswith(layer_name),
                    'expected url to end with %s, but got %s' %
                    (layer_name,
                     url))
                self.assertEqual(resp.status_code, 200)

                url = unquote(url)
                self.check_layer_complete(url, layer_name)
                wms = get_wms(
                    type_name='geonode:%s' % layer_name, username=GEOSERVER_USER, password=GEOSERVER_PASSWD)
                layer_info = list(wms.items())[0][1]
                self.assertEqual(100, len(layer_info.timepositions))
            else:
                self.assertTrue('error_msg' in resp_js)

    def test_configure_time(self):
        layer_name = 'boxes_with_end_date'
        # make sure it's not there (and configured)
        cascading_delete(layer_name=layer_name, catalog=gs_catalog)

        def get_wms_timepositions():
            alternate_name = 'geonode:%s' % layer_name
            if alternate_name in get_wms().contents:
                metadata = get_wms().contents[alternate_name]
                self.assertTrue(metadata is not None)
                return metadata.timepositions
            else:
                return None

        thefile = os.path.join(
            GOOD_DATA, 'time', '%s.shp' % layer_name
        )
        resp, data = self.client.upload_file(thefile)

        # initial state is no positions or info
        self.assertTrue(get_wms_timepositions() is None)
        self.assertEqual(resp.status_code, 200)

        # enable using interval and single attribute
        if not isinstance(data, string_types):
            self.wait_for_progress(data.get('progress'))
            self.assertTrue(data['success'])
            self.assertTrue(data['redirect_to'], upload_step('time'))
            redirect_to = data['redirect_to']
            resp, data = self.client.get_html(upload_step('time'))
            self.assertEqual(resp.status_code, 200)
            data = dict(csrfmiddlewaretoken=self.client.get_csrf_token(),
                        time_attribute='date',
                        time_end_attribute='enddate',
                        presentation_strategy='LIST',
                        )
            resp = self.client.make_request(redirect_to, data)
            self.assertEqual(resp.status_code, 200)
            resp_js = resp.json()
            if resp_js['success']:
                url = resp_js['redirect_to']

                resp = self.client.make_request(url, data)

                url = resp.json()['url']

                self.assertTrue(
                    url.endswith(layer_name),
                    'expected url to end with %s, but got %s' %
                    (layer_name,
                     url))
                self.assertEqual(resp.status_code, 200)

                url = unquote(url)
                self.check_layer_complete(url, layer_name)
                wms = get_wms(
                    type_name='geonode:%s' % layer_name, username=GEOSERVER_USER, password=GEOSERVER_PASSWD)
                layer_info = list(wms.items())[0][1]
                self.assertEqual(100, len(layer_info.timepositions))
            else:
                self.assertTrue('error_msg' in resp_js)
