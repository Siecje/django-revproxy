# -*- coding: utf-8 -*-

import os
import re
import sys
import mimetypes
import logging

import urllib3

from six.moves.urllib.parse import urljoin, urlparse, urlencode, quote

from django.shortcuts import redirect
from django.views.generic import View
from django.utils.decorators import classonlymethod

from .response import get_django_response
from .utils import normalize_headers, encode_items
from .transformer import DiazoTransformer


class ProxyView(View):
    add_remote_user = False
    diazo_theme_template = 'diazo.html'
    html5 = False
    rewrite = tuple() # It will be overrided by a tuple inside tuple.

    def __init__(self, *args, **kwargs):
        super(ProxyView, self).__init__(*args, **kwargs)

        self._rewrite = []
        # Take all elements inside tuple, and insert into _rewrite
        for from_pattern, to_pattern in self.rewrite:
            from_re = re.compile(from_pattern)
            self._rewrite.append((from_re, to_pattern))
        self.http = urllib3.PoolManager()
        self.log = logging.getLogger('revproxy.view')
        self.log.info("ProxyView created")

    @property
    def upstream(self):
        raise NotImplementedError('Upstream server must be set')

    @property
    def diazo_rules(self):
        child_class_file = sys.modules[self.__module__].__file__
        app_path = os.path.abspath(os.path.dirname(child_class_file))
        diazo_path = os.path.join(app_path, 'diazo.xml')

        self.log.debug("diazo_rules: {}".format(diazo_path))
        return diazo_path

    @classonlymethod
    def as_view(cls, **initkwargs):
        view = super(ProxyView, cls).as_view(**initkwargs)
        view.csrf_exempt = True
        return view

    def dispatch(self, request, path):

        # Rewrite implementation
        full_path = request.get_full_path()
        self.log.debug("Dispatch full path: {}".format(full_path))
        for from_re, to_pattern in self._rewrite:
            if from_re.match(full_path):
                redirect_to = from_re.sub(to_pattern, full_path)
                self.log.debug("Redirect to: {}".format(redirect_to))
                return redirect(redirect_to)

        request_payload = request.body
        request_headers = normalize_headers(request)
        self.log.debug("Request headers: {}".format(request_headers))

        if self.add_remote_user and request.user.is_active:
            request_headers['REMOTE_USER'] = request.user.username
            self.log.info("REMOTE_USER set")

        request_url = urljoin(
            self.upstream,
            quote(path.encode('utf8'))
        )
        self.log.debug("Request URL: {}".format(request_url))

        if request.GET:
            get_data = encode_items(request.GET.lists())
            request_url += '?' + urlencode(get_data)
            self.log.debug("Request URL: {}".format(request_url)) 

        try:
            proxy_response = self.http.urlopen(request.method,
                                               request_url,
                                               redirect=False,
                                               headers=request_headers,
                                               body=request_payload,
                                               decode_content=False,
                                               preload_content=False)
            self.log.debug("Proxy response header: {}".format(
                            proxy_response.getheaders()))
        except urllib3.exceptions.HTTPError as error:
            self.log.exception(error)
            raise

        location = proxy_response.headers.get('Location')
        if location:
            if request.is_secure():
                scheme = 'https://'
            else:
                scheme = 'http://'
            request_host = scheme + request.get_host()

            url = urlparse(self.upstream)
            upstream_host_http = 'http://' + url.netloc
            upstream_host_https = 'https://' + url.netloc

            location = location.replace(upstream_host_http, request_host)
            location = location.replace(upstream_host_https, request_host)
            proxy_response.headers['Location'] = location

            self.log.debug("Proxy response LOCATION: {}".format(
                                        proxy_response.headers['Location']))

        content_type = proxy_response.headers.get('Content-Type')
        if not content_type:
            content_type = (mimetypes.guess_type(request.path)[0] or
                            'application/octet-stream')
            proxy_response.headers['Content-Type'] = content_type
            self.log.debug("Proxy response CONTENT-TYPE: {}").format(
                                    proxy_response.headers['Content-Type'])

        response = get_django_response(proxy_response)

        if self.diazo_rules and self.diazo_theme_template:
            diazo = DiazoTransformer(request, response)
            response = diazo.transform(self.diazo_rules,
                                       self.diazo_theme_template,
                                       self.html5)

        self.log.debug("RESPONSE RETURNED: {}".format(response))
        return response
