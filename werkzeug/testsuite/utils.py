# -*- coding: utf-8 -*-
"""
    werkzeug.testsuite.utils
    ~~~~~~~~~~~~~~~~~~~~~~~~

    General utilities.

    :copyright: (c) 2014 by Armin Ronacher.
    :license: BSD, see LICENSE for more details.
"""

from __future__ import with_statement

import unittest
from datetime import datetime
from functools import partial

from werkzeug.testsuite import WerkzeugTestCase

from werkzeug import utils
from werkzeug.datastructures import Headers
from werkzeug.http import parse_date, http_date
from werkzeug.wrappers import BaseResponse
from werkzeug.test import Client, run_wsgi_app
from werkzeug._compat import text_type, implements_iterator


class GeneralUtilityTestCase(WerkzeugTestCase):

    def test_redirect_no_unicode_header_keys(self):
        # Make sure all headers are native keys.  This was a bug at one point
        # due to an incorrect conversion.
        resp = utils.redirect('http://example.com/', 305)
        for key, value in resp.headers.items():
            self.assert_equal(type(key), str)
            self.assert_equal(type(value), text_type)
        self.assert_equal(resp.headers['Location'], 'http://example.com/')
        self.assert_equal(resp.status_code, 305)

    def test_redirect_xss(self):
        location = 'http://example.com/?xss="><script>alert(1)</script>'
        resp = utils.redirect(location)
        self.assert_not_in(b'<script>alert(1)</script>', resp.get_data())

        location = 'http://example.com/?xss="onmouseover="alert(1)'
        resp = utils.redirect(location)
        self.assert_not_in(b'href="http://example.com/?xss="onmouseover="alert(1)"', resp.get_data())

    def test_validate_arguments(self):
        take_none = lambda: None
        take_two = lambda a, b: None
        take_two_one_default = lambda a, b=0: None

        self.assert_equal(utils.validate_arguments(take_two, (1, 2,), {}), ((1, 2), {}))
        self.assert_equal(utils.validate_arguments(take_two, (1,), {'b': 2}), ((1, 2), {}))
        self.assert_equal(utils.validate_arguments(take_two_one_default, (1,), {}), ((1, 0), {}))
        self.assert_equal(utils.validate_arguments(take_two_one_default, (1, 2), {}), ((1, 2), {}))

        self.assert_raises(utils.ArgumentValidationError,
            utils.validate_arguments, take_two, (), {})

        self.assert_equal(utils.validate_arguments(take_none, (1, 2,), {'c': 3}), ((), {}))
        self.assert_raises(utils.ArgumentValidationError,
               utils.validate_arguments, take_none, (1,), {}, drop_extra=False)
        self.assert_raises(utils.ArgumentValidationError,
               utils.validate_arguments, take_none, (), {'a': 1}, drop_extra=False)

    def test_header_set_duplication_bug(self):
        headers = Headers([
            ('Content-Type', 'text/html'),
            ('Foo', 'bar'),
            ('Blub', 'blah')
        ])
        headers['blub'] = 'hehe'
        headers['blafasel'] = 'humm'
        self.assert_equal(headers, Headers([
            ('Content-Type', 'text/html'),
            ('Foo', 'bar'),
            ('blub', 'hehe'),
            ('blafasel', 'humm')
        ]))

    def test_append_slash_redirect(self):
        def app(env, sr):
            return utils.append_slash_redirect(env)(env, sr)
        client = Client(app, BaseResponse)
        response = client.get('foo', base_url='http://example.org/app')
        self.assert_equal(response.status_code, 301)
        self.assert_equal(response.headers['Location'], 'http://example.org/app/foo/')

    def test_secure_filename(self):
        self.assert_equal(utils.secure_filename('My cool movie.mov'),
                          'My_cool_movie.mov')
        self.assert_equal(utils.secure_filename('../../../etc/passwd'),
                          'etc_passwd')
        self.assert_equal(utils.secure_filename(u'i contain cool \xfcml\xe4uts.txt'),
                          'i_contain_cool_umlauts.txt')


def suite():
    suite = unittest.TestSuite()
    suite.addTest(unittest.makeSuite(GeneralUtilityTestCase))
    return suite
