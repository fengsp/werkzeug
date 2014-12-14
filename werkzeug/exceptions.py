# -*- coding: utf-8 -*-
import sys

# Because of bootstrapping reasons we need to manually patch ourselves
# onto our parent module.
import werkzeug
werkzeug.exceptions = sys.modules[__name__]

from werkzeug._internal import _get_environ
from werkzeug._compat import iteritems, integer_types, text_type, \
     implements_to_string

from werkzeug.wrappers import Response


@implements_to_string
class HTTPException(Exception):
    """
    Baseclass for all HTTP exceptions.  This exception can be called as WSGI
    application to render a default error page or you can catch the subclasses
    of it independently and render nicer error messages.
    """

    def get_headers(self, environ=None):
        """Get a list of headers."""
        return [('Content-Type', 'text/html')]

    def get_response(self, environ=None):
        """Get a response object.  If one was passed to the exception
        it's returned directly.

        :param environ: the optional environ for the request.  This
                        can be used to modify the response depending
                        on how the request looked like.
        :return: a :class:`Response` object or a subclass thereof.
        """
        if self.response is not None:
            return self.response
        if environ is not None:
            environ = _get_environ(environ)
        headers = self.get_headers(environ)
        return Response(self.get_body(environ), self.code, headers)


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
