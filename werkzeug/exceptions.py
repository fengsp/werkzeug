# -*- coding: utf-8 -*-
"""
    werkzeug.exceptions
    ~~~~~~~~~~~~~~~~~~~

    This module implements a number of Python exceptions you can raise from
    within your views to trigger a standard non-200 response.


    Usage Example
    -------------

    ::

        from werkzeug.wrappers import BaseRequest
        from werkzeug.wsgi import responder
        from werkzeug.exceptions import HTTPException, NotFound

        def view(request):
            raise NotFound()

        @responder
        def application(environ, start_response):
            request = BaseRequest(environ)
            try:
                return view(request)
            except HTTPException as e:
                return e


    As you can see from this example those exceptions are callable WSGI
    applications.  Because of Python 2.4 compatibility those do not extend
    from the response objects but only from the python exception class.

    As a matter of fact they are not Werkzeug response objects.  However you
    can get a response object by calling ``get_response()`` on a HTTP
    exception.

    Keep in mind that you have to pass an environment to ``get_response()``
    because some errors fetch additional information from the WSGI
    environment.

    If you want to hook in a different exception page to say, a 404 status
    code, you can add a second except for a specific subclass of an error::

        @responder
        def application(environ, start_response):
            request = BaseRequest(environ)
            try:
                return view(request)
            except NotFound, e:
                return not_found(request)
            except HTTPException, e:
                return e


    :copyright: (c) 2014 by the Werkzeug Team, see AUTHORS for more details.
    :license: BSD, see LICENSE for more details.
"""
import sys

# Because of bootstrapping reasons we need to manually patch ourselves
# onto our parent module.
import werkzeug
werkzeug.exceptions = sys.modules[__name__]

from werkzeug._internal import _get_environ
from werkzeug._compat import iteritems, integer_types, text_type, \
     implements_to_string


class MethodNotAllowed(HTTPException):
    """*405* `Method Not Allowed`

    Raise if the server used a method the resource does not handle.  For
    example `POST` if the resource is view only.  Especially useful for REST.

    The first argument for this exception should be a list of allowed methods.
    Strictly speaking the response would be invalid if you don't provide valid
    methods in the header which you can do with that list.
    """
    code = 405
    description = 'The method is not allowed for the requested URL.'

    def __init__(self, valid_methods=None, description=None):
        """Takes an optional list of valid http methods
        starting with werkzeug 0.3 the list will be mandatory."""
        HTTPException.__init__(self, description)
        self.valid_methods = valid_methods

    def get_headers(self, environ):
        headers = HTTPException.get_headers(self, environ)
        if self.valid_methods:
            headers.append(('Allow', ', '.join(self.valid_methods)))
        return headers


default_exceptions = {}
__all__ = ['HTTPException']

def _find_exceptions():
    for name, obj in iteritems(globals()):
        try:
            if getattr(obj, 'code', None) is not None:
                default_exceptions[obj.code] = obj
                __all__.append(obj.__name__)
        except TypeError: # pragma: no cover
            continue
_find_exceptions()
del _find_exceptions


class Aborter(object):
    """
    When passed a dict of code -> exception items it can be used as
    callable that raises exceptions.  If the first argument to the
    callable is an integer it will be looked up in the mapping, if it's
    a WSGI application it will be raised in a proxy exception.

    The rest of the arguments are forwarded to the exception constructor.
    """

    def __init__(self, mapping=None, extra=None):
        if mapping is None:
            mapping = default_exceptions
        self.mapping = dict(mapping)
        if extra is not None:
            self.mapping.update(extra)

    def __call__(self, code, *args, **kwargs):
        if not args and not kwargs and not isinstance(code, integer_types):
            raise HTTPException(response=code)
        if code not in self.mapping:
            raise LookupError('no exception for %r' % code)
        raise self.mapping[code](*args, **kwargs)

abort = Aborter()


#: an exception that is used internally to signal both a key error and a
#: bad request.  Used by a lot of the datastructures.
BadRequestKeyError = BadRequest.wrap(KeyError)


# imported here because of circular dependencies of werkzeug.utils
from werkzeug.utils import escape
from werkzeug.http import HTTP_STATUS_CODES
