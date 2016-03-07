'''===========================================================================
15-Oct-2015    josep.sampe    Initial implementation.
==========================================================================='''
from SBusPythonFacade.SBus import SBus
from SBusPythonFacade.SBusDatagram import SBusDatagram
from eventlet.timeout import Timeout
import vertigo_common as cc
import os
import select
import json
import subprocess
import time

SBUS_FD_INPUT_OBJECT = 0
SBUS_FD_OUTPUT_OBJECT = 1
SBUS_FD_OUTPUT_OBJECT_METADATA = 2
SBUS_FD_LOGGER = 4

SBUS_CMD_EXECUTE = 1

MC_MAIN_HEADER = "X-Object-Meta-Handler-Main"
MC_DEP_HEADER = "X-Object-Meta-Handler-Library-Dependency"
DEFAULT_MD_STRING = {'onget': 'None',
                     'onput': 'None',
                     'ondelete': 'None',
                     'ontimer': 'None'}


class VertigoGatewayDocker():

    def __init__(self, req, orig_resp, conf, logger, app, version,
                 account, container, obj):
        self.req = req
        self.orig_resp = orig_resp
        self.conf = conf
        self.logger = logger
        self.app = app
        self.version = version
        self.account = account
        self.container = container
        self.obj = obj
        self.scope = account[5:18]
        self.file_path = None
        self.current_server = self.conf["execution_server"]
        self.mc_timeout = self.conf["mc_timeout"]
        self.mc_container = self.conf["mc_container"]
        self.mc_dependency = self.conf["mc_dependency"]
        self.mc_list = None
        self.mc_metadata = dict()

        # CONTAINER
        self.docker_img_prefix = "vertigo"
        self.docker_repo = conf['docker_repo']

    def start_internal_client_daemon(self):
        self.logger.info('Vertigo - Starting Internal Client ...')

        pid = os.popen("ps -aef | grep -i 'internal_client_daemon.py' | grep" +
                       " -v 'grep' | awk '{ print $2 }'").read()

        if pid != "":
            self.logger.info('Vertigo - Internal Client is already' +
                             ' started')
        else:
            # TODO: Change IC path
            cmd = '/usr/bin/python /opt/urv/internal_client_daemon.py ' \
                '/home/lxc_device/pipes/scopes/bd34c4073b654/internal_client_pipe DEBUG &'

            self.logger.info(cmd)

            # TODO: Call external script
            p = subprocess.call(cmd, shell=True)

            print p

            if p == 0:
                self.logger.info('Vertigo - Internal Client daemon' +
                                 ' started')
            else:
                self.logger.info('Vertigo - Error starting Internal' +
                                 ' Client daemon')

            time.sleep(1)

    def start_container(self):

        # Extract the account's ID from the account
        if self.account.lower().startswith('auth_'):
            account_id = self.account[len('auth_'):]
        else:
            account_id = self.account

        docker_container_name = '%s_%s' % (self.docker_img_prefix, account_id)
        docker_image_name = '%s/%s' % (self.docker_repo, account_id)

        host_pipe_prefix = self.conf["pipes_dir"] + "/" + self.scope
        sandbox_pipe_prefix = "/mnt/channels"

        pipe_mount = '%s:%s' % (host_pipe_prefix, sandbox_pipe_prefix)

        host_storlet_prefix = self.conf["mc_dir"] + "/" + self.scope
        sandbox_storlet_dir_prefix = "/home/swift"

        mc_mount = '%s:%s' % (host_storlet_prefix,
                              sandbox_storlet_dir_prefix)

        cmd = "sudo docker run --net=none --name " + docker_container_name + \
              " -d -v /dev/log:/dev/log -v " + pipe_mount + " -v " + mc_mount + \
              " -i -t " + docker_image_name + " debug /home/swift/start_daemon.sh"

        # self.logger.info(cmd)

        self.logger.info('Vertigo - Starting container '
                         + docker_container_name + ' ...')

        p = subprocess.call(cmd, shell=True)

        if p == 0:
            time.sleep(1)
            self.logger.info('Vertigo - Container ' +
                             docker_container_name + ' started')
        else:
            self.logger.info('Vertigo - Container ' +
                             docker_container_name + ' is already started')

    def set_microcontroller(self, trigger, mc):
        trigger = trigger.rsplit('-', 1)[1].lower()

        # We need a GET to know where is the object
        get_req = self.req.copy_get()
        get_resp = get_req.get_response(self.app)

        fd = get_resp.app_iter._fp

        object_mc_md = cc.read_metadata(fd)
        if not object_mc_md:
            object_mc_md = DEFAULT_MD_STRING
        object_mc_md[trigger] = mc
        cc.write_metadata(fd, object_mc_md)

        # Write micro-controller metadata file
        file_path = get_resp.app_iter._data_file.rsplit('/', 1)[0]
        self.logger.info('Vertigo - File path: ' + file_path)
        metadata_target_path = os.path.join(file_path,
                                            mc.rsplit('.', 1)[0] + ".md")
        fn = open(metadata_target_path, 'w')
        fn.write(self.req.body)
        fn.close()

        self.logger.info('Vertigo - File path: ' + file_path)

    def set_microcontroller_list(self, mc_list):
        self.mc_list = mc_list.split(",")

    def get_microcontrollers(self):

        req = self.orig_resp.environ["REQUEST_METHOD"]
        fd = self.orig_resp.app_iter._fp

        controller_md = cc.read_metadata(fd)

        if controller_md:
            self.mc_list = controller_md["on" + req.lower()].split(",")
            if self.mc_list == 'None':
                return False
            return True
        else:
            return False

    def execute_microcontrollers(self, server=None):

        # We need to start Internal CLient
        self.start_internal_client_daemon()  # each tenat their own IC
        # We need to start container if it is stopped
        self.start_container()  # TODO: NO SEMPRE

        """
        if server == "proxy":
            self.file_path = "/tmp/"
        else:
        """

        self.file_path = self.orig_resp.app_iter._data_file.rsplit('/', 1)[0]

        # Verify access to micro-controllers and dependencies, and update cache
        # TODO: Update cache only if node doesn't have the MCF
        for mc_name in self.mc_list:
            mc_verified = self.verify_access(self.mc_container, mc_name)

            if mc_verified:
                self.update_mc_cache(self.mc_container, mc_name, mc_name)
                dep_list = self.mc_metadata[mc_name][MC_DEP_HEADER].split(",")
                for dep in dep_list:
                    dep_verified = self.verify_access(self.mc_dependency, dep)
                    if dep_verified:
                        self.update_mc_cache(self.mc_dependency, mc_name, dep)
                    else:
                        self.logger.error('Vertigo - Dependency ' +
                                          dep + " not found in Swift")
                        raise NameError("MicroController - Dependency " +
                                        dep + " not found in Swift")
            else:
                raise NameError("MicroController - Micro-controller " +
                                mc_name + " not found in Swift")

        mc_logger_path = self.conf["log_dir"] + "/" + self.scope + "/"
        mc_pipe_path = self.conf["pipes_dir"] + "/" + self.scope + "/" + \
            self.conf["mc_pipe"]

        self.logger.info('Vertigo - Object path: ' + self.file_path)

        self.req.headers['X-Current-Server'] = self.current_server

        protocol = MicroControllerInvocationProtocol(self.file_path,
                                                     mc_pipe_path,
                                                     mc_logger_path,
                                                     dict(self.req.headers),
                                                     self.orig_resp.headers,
                                                     self.mc_list,
                                                     self.mc_metadata,
                                                     self.mc_timeout,
                                                     self.logger)

        return protocol.communicate()

    def verify_access(self, container, mc_name):
        resp = cc.make_swift_request("HEAD", self.account, container, mc_name)

        if resp.status_int < 300 and resp.status_int >= 200:
            if container == self.mc_container:
                self.mc_metadata[mc_name] = resp.headers
            return True

        return False

    def update_mc_cache(self, container, mc_name, obj):
        resp = cc.make_swift_request("GET", self.account, container, obj)

        docker_mc_path = self.conf["mc_dir"] + "/" + self.scope + \
            "/" + self.mc_metadata[mc_name][MC_MAIN_HEADER]

        docker_target_path = os.path.join(docker_mc_path, obj)
        if not os.path.exists(docker_mc_path):
            os.makedirs(docker_mc_path, 0o755)

        fn = open(docker_target_path, 'w')
        fn.write(resp.body)
        fn.close()


class MicroControllerInvocationProtocol(object):

    def __init__(self, file_path, mc_pipe_path, mc_logger_path, req_haders,
                 file_headers, mc_list, mc_metadata, timeout, logger):
        self.logger = logger
        self.mc_pipe_path = mc_pipe_path
        self.mc_logger_path = mc_logger_path
        self.timeout = timeout
        self.req_md = req_haders
        self.file_md = file_headers
        self.mc_list = mc_list  # Micro-controller name list
        self.mc_md = mc_metadata  # Micro-controller metadata
        self.file_path = file_path  # Path of requested object
        self.micro_controllers = list()  # Micro-controller object list

        # remote side file descriptors and their metadata lists
        # to be sent as part of invocation
        self.fds = list()
        self.fdmd = list()

        # local side file descriptors
        self.response_read_fd = None
        self.response_write_fd = None
        self.null_read_fd = None
        self.null_write_fd = None
        self.task_id = None

    def _add_output_stream(self):
        self.fds.append(self.response_write_fd)
        md = dict()
        md['type'] = SBUS_FD_OUTPUT_OBJECT
        self.fdmd.append(md)

    def _add_logger_stream(self):
        for mc in self.micro_controllers:
            self.fds.append(mc.get_logfd())
            md = dict()
            md['type'] = SBUS_FD_LOGGER
            md['handler'] = mc.get_name()
            self.fdmd.append(md)

    def _add_metadata_stream(self):  # ADDED
        for mc in self.micro_controllers:
            self.fds.append(mc.get_mdfd())
            md = dict()
            md['type'] = SBUS_FD_OUTPUT_OBJECT_METADATA
            md['handler'] = mc.get_name()
            md['main'] = mc.get_main()
            md['dependencies'] = mc.get_dependencies()
            self.fdmd.append(md)

    def _add_file_req_md(self):
        self.fds.append(self.null_write_fd)
        if "X-Service-Catalog" in self.req_md:
            del self.req_md['X-Service-Catalog']

        if "Cookie" in self.req_md:
            del self.req_md['Cookie']

        headers = {'req_md': self.req_md, 'file_md': self.file_md}

        md = dict()
        md['type'] = SBUS_FD_INPUT_OBJECT
        md['json_md'] = json.dumps(headers)
        self.fdmd.append(md)

    def _prepare_invocation_descriptors(self):
        # Add the response stream
        self.response_read_fd, self.response_write_fd = os.pipe()
        self.null_read_fd, self.null_write_fd = os.pipe()

        # Add req and file headers
        self._add_file_req_md()
        # Add output pipe
        self._add_output_stream()
        # Add the loggers
        self._add_logger_stream()
        # Add the metadata files
        self._add_metadata_stream()

    def _close_remote_side_descriptors(self):
        if self.response_write_fd:
            os.close(self.response_write_fd)

    def _invoke(self):
        dtg = SBusDatagram()
        dtg.set_files(self.fds)
        dtg.set_metadata(self.fdmd)
        # dtg.set_exec_params(prms)
        dtg.set_command(SBUS_CMD_EXECUTE)

        # Send datagram to container daemon
        rc = SBus.send(self.mc_pipe_path, dtg)
        if (rc < 0):
            raise Exception("Failed to send execute command")

    def _wait_for_read_with_timeout(self, fd):
        r, _, _ = select.select([fd], [], [], self.timeout)
        if len(r) == 0:
            if self.task_id:
                self._cancel()
            raise Timeout('Timeout while waiting for Micro-controller output')
        if fd in r:
            return

    def _read_response(self):
        self._wait_for_read_with_timeout(self.response_read_fd)
        flat_json = os.read(self.response_read_fd, 1024)

        if flat_json == "{}":
            out_data = None
        else:
            out_data = json.loads(flat_json)

        return out_data

    def communicate(self):
        for mc_name in self.mc_list:
            mc = MicroController(self.file_path,
                                 self.mc_logger_path,
                                 mc_name,
                                 self.mc_md[mc_name][MC_MAIN_HEADER],
                                 self.mc_md[mc_name][MC_DEP_HEADER])
            self.micro_controllers.append(mc)

        for mc in self.micro_controllers:
            mc.open()

        self._prepare_invocation_descriptors()

        try:
            self._invoke()
        except Exception as e:
            raise e
        finally:
            self._close_remote_side_descriptors()
            for mc in self.micro_controllers:
                mc.close()

        out_data = self._read_response()
        os.close(self.response_read_fd)

        return out_data


class MicroController(object):

    def __init__(self, file_path, logger_path, name, main, dependencies):

        self.full_md_path = os.path.join(file_path, '%s.md' %
                                         name.rsplit('.', 1)[0])
        self.full_log_path = os.path.join(logger_path, '%s/%s.log' %
                                          (main,  name.rsplit('.', 1)[0]))
        self.micro_controller = name
        self.main_class = main
        self.dependencies = dependencies

        if not os.path.exists(os.path.join(logger_path, '%s' % main)):
            os.makedirs(os.path.join(logger_path, '%s' % main))

    def open(self):
        self.metadata_file = open(self.full_md_path, 'a+')
        self.logger_file = open(self.full_log_path, 'a')

    def get_mdfd(self):
        return self.metadata_file.fileno()

    def get_logfd(self):
        return self.logger_file.fileno()

    def get_name(self):
        return self.micro_controller

    def get_dependencies(self):
        return self.dependencies

    def get_main(self):
        return self.main_class

    def get_size(self):
        statinfo = os.stat(self.full_path)
        return statinfo.st_size

    def close(self):
        self.metadata_file.close()
        self.logger_file.close()