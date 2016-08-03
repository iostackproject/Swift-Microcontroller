from swift.proxy.controllers.base import get_account_info
from swift.common.swob import HTTPUnauthorized, HTTPBadRequest, Range
from swift.common.utils import config_true_value

from vertigo_middleware.gateways import VertigoGatewayDocker, VertigoGatewayStorlet


class NotVertigoRequest(Exception):
    pass


def _request_instance_property():
    """
    Set and retrieve the request instance.
    This works to force to tie the consistency between the request path and
    self.vars (i.e. api_version, account, container, obj) even if unexpectedly
    (separately) assigned.
    """

    def getter(self):
        return self._request

    def setter(self, request):
        self._request = request
        try:
            self._extract_vaco()
        except ValueError:
            raise NotVertigoRequest()

    return property(getter, setter,
                    doc="Force to tie the request to acc/con/obj vars")


class VertigoBaseHandler(object):
    """
    This is an abstract handler for Proxy/Object Server middleware
    """
    request = _request_instance_property()

    def __init__(self, request, conf, app, logger):
        """
        :param request: swob.Request instance
        :param conf: gatway conf dict
        """
        self.request = request
        self.vertigo_containers = [conf.get('mc_container'),
                                   conf.get('mc_dependency'),
                                   conf.get('storlet_container'),
                                   conf.get('storlet_dependency')]
        self.available_assignation_headers = ['X-Vertigo-Onget',
                                              'X-Vertigo-Ondelete',
                                              'X-Vertigo-Onput',
                                              'X-Vertigo-Ontimer']   
        self.available_deletion_headers = ['X-Vertigo-Onget-Delete',
                                           'X-Vertigo-Ondelete-Delete',
                                           'X-Vertigo-Onput-Delete',
                                           'X-Vertigo-Ontimer-Delete',
                                           'X-Vertigo-Delete']
        
        self.app = app
        self.logger = logger
        self.conf = conf
        self.method = self.request.method.lower()
        self.execution_server = conf["execution_server"]

    def _setup_docker_gateway(self, response):
        self.mc_docker_gateway = VertigoGatewayDocker(
            self.request, response,
            self.conf, self.logger, self.account)

    def _setup_storlet_gateway(self):
        self.storlet_gateway = VertigoGatewayStorlet(
            self.conf, self.logger, self.app, self.api_version,
            self.account, self.request.method)

    def _extract_vaco(self):
        """
        Set version, account, container, obj vars from self._parse_vaco result
        :raises ValueError: if self._parse_vaco raises ValueError while
                            parsing, this method doesn't care and raise it to
                            upper caller.
        """
        self._api_version, self._account, self._container, self._obj = \
            self._parse_vaco()

    def get_mc_assignation_data(self):
        header = [i for i in self.available_assignation_headers
                  if i in self.request.headers.keys()]
        if len(header) > 1:
            raise HTTPUnauthorized('Vertigo - The system can only set 1'\
                                   ' microcontroller each time.\n')
        mc = self.request.headers[header[0]]
        
        return header[0].rsplit('-', 1)[1].lower(), mc
    
    def get_mc_deletion_data(self):
        header = [i for i in self.available_deletion_headers
                  if i in self.request.headers.keys()]
        if len(header) > 1:
            raise HTTPUnauthorized('Vertigo - The system can only delete 1'\
                                   ' microcontroller each time.\n')
        mc = self.request.headers[header[0]]

        return header[0].rsplit('-', 2)[1].lower(), mc   

    @property
    def api_version(self):
        return self._api_version

    @property
    def account(self):
        return self._account

    @property
    def container(self):
        return self._container

    @property
    def obj(self):
        return self._obj

    def _parse_vaco(self):
        """
        Parse method of path from self.request which depends on child class
        (Proxy or Object)
        
        :return tuple: a string tuple of (version, account, container, object)
        """
        raise NotImplementedError()

    def handle_request(self):
        """
        Run Vertigo
        """
        raise NotImplementedError()

    @property
    def is_storlet_execution(self):
        """
        Check if the request requires storlet execution
        
        :return: Whether storlet should be executed
        """
        return 'X-Run-Storlet' in self.request.headers

    @property
    def is_range_request(self):
        """
        Determines whether the request is a byte-range request
        """
        return 'Range' in self.request.headers
    
    @property
    def is_storlet_range_request(self):
        return 'X-Storlet-Range' in self.request.headers
    
    @property
    def is_storlet_multiple_range_request(self):
        if not self.is_storlet_range_request:
            return False

        r = self.request.headers['X-Storlet-Range']
        return len(Range(r).ranges) > 1
    
    @property
    def is_vertigo_container_request(self):
        """
        Determines whether the request is over any vertigo container
        """
        pass

    @property
    def is_trigger_assignation(self):
        return any((True for x in self.available_assignation_headers
                    if x in self.request.headers.keys()))

    @property
    def is_trigger_deletion(self):
        return any((True for x in self.available_deletion_headers
                    if x in self.request.headers.keys()))
        
    def is_slo_response(self, resp):
        self.logger.debug(
            'Verify if {0}/{1}/{2} is an SLO assembly object'.format(
                self.account, self.container, self.obj))
        is_slo = 'X-Static-Large-Object' in resp.headers
        if is_slo:
            self.logger.debug(
                '{0}/{1}/{2} is indeed an SLO assembly '
                'object'.format(self.account, self.container, self.obj))
        else:
            self.logger.debug(
                '{0}/{1}/{2} is NOT an SLO assembly object'.format(
                    self.account, self.container, self.obj))
        return is_slo

    def is_account_storlet_enabled(self):
        account_meta = get_account_info(self.request.environ,
                                        self.app)['meta']
        storlets_enabled = account_meta.get('storlet-enabled',
                                            'False')
        if not config_true_value(storlets_enabled):
            self.logger.debug('Vertigo - Account disabled for storlets')
            return HTTPBadRequest('Vertigo - Account disabled for storlets',
                                  request=self.request)

        return True

    def _call_storlet_gateway(self, resp):
        """
        Call gateway module to get result of storlet execution
        in GET flow
        """
        raise NotImplementedError()

    def apply_storlet_on_get(self, resp, storlet_list):
        resp = self._call_storlet_gateway_on_get(resp, storlet_list)

        if 'Content-Length' in resp.headers:
            resp.headers.pop('Content-Length')
        if 'Transfer-Encoding' in resp.headers:
            resp.headers.pop('Transfer-Encoding')

        return resp

    def apply_storlet_on_put(self, req, storlet_list):
        self.request = self._call_storlet_gateway_on_put(req, storlet_list)

        if 'CONTENT_LENGTH' in self.request.environ:
            self.request.environ.pop('CONTENT_LENGTH')
        self.request.headers['Transfer-Encoding'] = 'chunked'
