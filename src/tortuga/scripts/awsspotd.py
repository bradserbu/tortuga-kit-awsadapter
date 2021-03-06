#!/usr/bin/env python

# Copyright 2008-2018 Univa Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import configparser
import json
import logging
import optparse
import sys
import threading
import time

import boto
import boto.ec2
import gevent
import gevent.queue
import zmq
from daemonize import Daemonize

from tortuga.exceptions.nodeAlreadyExists import NodeAlreadyExists
from tortuga.exceptions.nodeNotFound import NodeNotFound
from tortuga.hardwareprofile.hardwareProfileApi import HardwareProfileApi
from tortuga.node.nodeApi import NodeApi
from tortuga.node import state
from tortuga.wsapi.addHostWsApi import AddHostWsApi


PIDFILE = '/var/log/awsspotd.pid'

# Poll for spot instance status every 60s
SPOT_INSTANCE_POLLING_INTERVAL = 60

spot_cache = threading.RLock()

spot_instance_request_cache_filename = \
    '/opt/tortuga/var/spot-instances.conf'


def refresh_spot_instance_request_cache():
    cfg = configparser.ConfigParser()

    cfg.read(spot_instance_request_cache_filename)

    return cfg


def write_spot_instance_request_cache(cfg):
    with open(spot_instance_request_cache_filename, 'w') as fp:
        cfg.write(fp)


def update_spot_instance_request_cache(sir_id, metadata=None):
    if not metadata:
        return

    cfg = refresh_spot_instance_request_cache()

    if not cfg.has_section(sir_id):
        cfg.add_section(sir_id)

    for key, value in list(metadata.items()):
        cfg.set(sir_id, key, value)

    write_spot_instance_request_cache(cfg)


def listener(logger):
    logger.info('Starting listener thread')

    context = zmq.Context()
    socket = context.socket(zmq.REP)
    socket.bind("tcp://*:5555")

    while True:
        message = socket.recv()

        request = json.loads(message)

        if 'action' not in request:
            socket.send(json.dumps({'error': 'malformed request'}))

            continue

        with spot_cache:
            cfg = refresh_spot_instance_request_cache()

            if not cfg.has_section(request['spot_instance_request_id']):
                cfg.add_section(request['spot_instance_request_id'])

                logger.info(
                    'Updating spot instance [{0}]'.format(
                        request['spot_instance_request_id']))

                if 'softwareprofile' in request:
                    cfg.set(request['spot_instance_request_id'],
                            'softwareprofile', request['softwareprofile'])

                if 'hardwareprofile' in request:
                    cfg.set(request['spot_instance_request_id'],
                            'hardwareprofile', request['hardwareprofile'])

                if 'resource_adapter_configuration' in request:
                    cfg.set(request['spot_instance_request_id'],
                            'resource_adapter_configuration',
                            request['resource_adapter_configuration'])

                write_spot_instance_request_cache(cfg)

        #  Send reply back to client
        socket.send(b"success")


class AWSSpotdAppClass(object):
    def __init__(self, options, args):
        self.options = options
        self.args = args

        self.logger = None

    def run(self):
        # Ensure logger is instantiated _after_ process is daemonized
        self.logger = logging.getLogger('tortuga.aws.awsspotd')

        self.logger.setLevel(logging.DEBUG)

        # create console handler and set level to debug
        ch = logging.handlers.TimedRotatingFileHandler(
            '/var/log/tortuga_awsspotd', when='midnight')
        ch.setLevel(logging.DEBUG)

        # create formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s')

        # add formatter to ch
        ch.setFormatter(formatter)

        # add ch to logger
        self.logger.addHandler(ch)

        self.logger.info(
            'Starting... EC2 region [{0}]'.format(self.options.region))

        # Create thread for message queue requests from resource adapter
        listener_thread = threading.Thread(
            target=listener, args=(self.logger,))
        listener_thread.daemon = True
        listener_thread.start()

        queue = gevent.queue.JoinableQueue()

        while True:
            cfg = refresh_spot_instance_request_cache()

            # Spawn coroutines to process spot instance requests
            num_workers = 20

            for thread_id in range(num_workers):
                gevent.spawn(self.worker, thread_id, queue)

            spot_instance_requests = \
                self.__parse_spot_instance_request_cache(cfg)
            if spot_instance_requests:
                # Enqueue spot instance requests for processing
                for spot_instance_request in spot_instance_requests:
                    queue.put(spot_instance_request)

                # Process spot instance requests
                queue.join()

                # Clean up
                self.logger.info('Cleaning up...')

                with spot_cache:
                    cfg = refresh_spot_instance_request_cache()

                    for spot_instance_request in spot_instance_requests:
                        if 'status' not in spot_instance_request:
                            continue

                        sir_status = spot_instance_request['status']

                        if sir_status not in \
                                ('invalid', 'notfound', 'cancelled',
                                 'terminated'):
                            continue

                        # Delete spot instance request cache entry

                        self.logger.info(
                            'Removing spot instance request [{0}]'.format(
                                spot_instance_request['sir_id']))

                        cfg.remove_section(
                            spot_instance_request['sir_id'])

                    # Rewrite spot instance request cache
                    write_spot_instance_request_cache(cfg)
            else:
                self.logger.info('No spot instance requests to process')

            self.logger.info('Sleeping for %ds' % (
                self.options.polling_interval))

            time.sleep(self.options.polling_interval)

    def __parse_spot_instance_request_cache(self, cfg):
        spot_instance_requests = []

        for sir_id in cfg.sections():
            spot_instance_request = {
                'sir_id': sir_id,
            }

            if cfg.has_option(sir_id, 'softwareprofile'):
                spot_instance_request['softwareprofile'] = \
                    cfg.get(sir_id, 'softwareprofile')

            if cfg.has_option(sir_id, 'hardwareprofile'):
                spot_instance_request['hardwareprofile'] = \
                    cfg.get(sir_id, 'hardwareprofile')

            if cfg.has_option(sir_id, 'resource_adapter_configuration'):
                spot_instance_request['resource_adapter_configuration'] = \
                    cfg.get(sir_id, 'resource_adapter_configuration')

            spot_instance_requests.append(spot_instance_request)

        return spot_instance_requests

    def worker(self, thread_id, queue): \
            # pylint: disable=unused-argument
        while True:
            try:
                spot_instance_request = queue.get()

                self.process_spot_instance_request(spot_instance_request)
            except Exception:
                self.logger.exception(
                    'Exception while processing spot instance request')
            finally:
                queue.task_done()

    def process_spot_instance_request(self, spot_instance_request):
        """
        Raises:
            EC2ResponseError
        """

        hwp = HardwareProfileApi().getHardwareProfile(
            spot_instance_request['hardwareprofile'])

        ec2_conn = boto.ec2.connect_to_region(self.options.region)

        sir_id = spot_instance_request['sir_id']

        try:
            result = ec2_conn.get_all_spot_instance_requests(
                request_ids=[sir_id])
        except boto.exception.EC2ResponseError as exc:
            if exc.status == 400 and \
                    exc.error_code == u'InvalidSpotInstanceRequestID.NotFound':
                spot_instance_request['status'] = 'notfound'

            raise

        self.logger.info(
            'sir: [{0}], state: [{1}],'
            ' status code: [{2}]'.format(
                sir_id, result[0].state, result[0].status.code))

        if result[0].state == 'active':
            if result[0].status.code == 'fulfilled':
                cfg = refresh_spot_instance_request_cache()

                if cfg.has_section(sir_id) and \
                        cfg.has_option(sir_id, 'status') and \
                        cfg.get(sir_id, 'status') == 'fulfilled':
                    # sir has already been processed
                    return

                self.__fulfilled_request_handler(
                    ec2_conn, sir_id, result[0].instance_id,
                    spot_instance_request, hwp)
        elif result[0].state == 'open':
            if result[0].status.code == 'pending-fulfillment':
                self.logger.info('{0}'.format(result))
            elif result[0].status.code == 'price-too-low':
                #  request price-too-low
                pass
            elif result[0].status.code == 'instance-terminated-by-price':
                # TODO: persistent spot instance request terminated due to
                # price increase; queue delete node request
                self.delete_node(sir_id)

                spot_instance_request['status'] = 'terminated'
            elif result[0].status.code == 'instance-terminated-no-capacity':
                self.delete_node(sir_id)

                spot_instance_request['status'] = 'terminated'
            elif result[0].status.code == \
                    'instance-terminated-capacity-oversubscribed':
                self.delete_node(sir_id)

                spot_instance_request['status'] = 'terminated'
            elif result[0].status.code == \
                    'instance-terminated-launch-group-constraint':
                self.delete_node(sir_id)

                spot_instance_request['status'] = 'terminated'
        elif result[0].state == 'closed':
            if result[0].status.code == 'marked-for-termination':
                # TODO: any hinting for Tortuga here?
                self.logger.info(
                    'Instance {0} marked for termination'.format(
                        result[0].instance_id))
            elif result[0].status.code == 'instance-terminated-by-user':
                # TODO: instance was terminated by user, but the spot request
                # was not cancelled

                self.delete_node(sir_id)

                spot_instance_request['status'] = 'terminated'
            elif result[0].status.code == 'instance-terminated-by-price':
                # TODO: one-time spot instance request price increased;
                # queue delete node request
                self.delete_node(sir_id)

                # Mark spot instance request for deletion
                spot_instance_request['status'] = 'terminated'
            elif result[0].status.code == 'instance-terminated-no-capacity':
                self.delete_node(sir_id)

                spot_instance_request['status'] = 'terminated'
            elif result[0].status.code == \
                    'instance-terminated-capacity-oversubscribed':
                self.delete_node(sir_id)

                spot_instance_request['status'] = 'terminated'
            elif result[0].status.code == \
                    'instance-terminated-launch-group-constraint':
                self.delete_node(sir_id)

                spot_instance_request['status'] = 'terminated'
            elif result[0].status.code == 'system-error':
                # TODO: nothing else can be done here; abort...
                pass
        elif result[0].state == 'cancelled':
            if result[0].status.code == 'canceled-before-fulfillment':
                # TODO: request was cancelled by end-user; nothing to do here

                spot_instance_request['status'] = 'cancelled'
            elif result[0].status.code == \
                    'request-canceled-and-instance-running':
                # Instance was left running after cancelling spot reqest;
                # nothing to do...
                pass
            elif result[0].status.code == 'instance-terminated-by-user':
                # TODO: queue delete node request

                self.delete_node(sir_id)

                spot_instance_request['status'] = 'terminated'
            elif result[0].status.code == \
                    'instance-terminated-capacity-oversubscribed':
                self.delete_node(sir_id)

                spot_instance_request['status'] = 'terminated'
        elif result[0].state == 'failed':
            # TODO: this request is dead in the water; nothing more can happen
            pass

    def __fulfilled_request_handler(self, ec2_conn, sir_id, instance_id,
                                    spot_instance_request, hwp):
        # Ensure node entries created
        resvs = ec2_conn.get_all_instances(instance_ids=[instance_id])

        instance = resvs[0].instances[0]

        if instance.state not in ('pending', 'running'):
            self.logger.info(
                'Ignoring instance [{0}] in state [{1}]'.format(
                    instance.id, instance.state))

            return

        # Determine node from spot instance request id
        adapter_cfg = configparser.ConfigParser()
        adapter_cfg.read('/opt/tortuga/var/aws-instance.conf')

        create_node = False

        for node_name in adapter_cfg.sections():
            if adapter_cfg.has_option(
                    node_name, 'spot_instance_request') and \
                    adapter_cfg.get(node_name,
                                    'spot_instance_request') == sir_id:
                break
        else:
            create_node = True

            node_name = instance.private_dns_name \
                if hwp.getNameFormat() == '*' else None

        if create_node:
            self.logger.info(
                'Creating node for spot instance'
                ' [{0}]'.format(instance.id))

            # Error: unable to find pre-allocated node record for spot
            # instance request
            addNodesRequest = {
                'softwareProfile':
                    spot_instance_request['softwareprofile'],
                'hardwareProfile':
                    spot_instance_request['hardwareprofile'],
                'isIdle': False,
                'count': 1,
                'nodeDetails': [{
                    'metadata': {
                        'ec2_instance_id': instance.id,
                        'ec2_ipaddress': instance.private_ip_address,
                    }
                }],
            }

            if 'resource_adapter_configuration' in spot_instance_request:
                addNodesRequest['resource_adapter_configuration'] = \
                    spot_instance_request['resource_adapter_configuration']

            if node_name:
                addNodesRequest['nodeDetails'][0]['name'] = node_name

            try:
                addHostSession = AddHostWsApi().addNodes(addNodesRequest)

                with gevent.Timeout(300):
                    while True:
                        response = AddHostWsApi()\
                            .getStatus(session=addHostSession, getNodes=True)

                        if not response['running']:
                            self.logger.debug('response: {0}'.format(response))
                            node_name = response['nodes'][0]['name']
                            break

                        gevent.sleep(5)
            except gevent.timeout.Timeout:
                self.logger.error(
                    'Timeout waiting for add nodes operation'
                    ' to complete')
            except NodeAlreadyExists:
                self.logger.error(
                    'Error adding node [{0}]:'
                    ' already exists'.format(instance.private_dns_name))
        else:
            self.logger.info(
                'Updating existing node [{0}]'.format(
                    node_name))

            # Mark node as 'Provisioned' now that there's a backing instance
            NodeApi().updateNode(node_name, updateNodeRequest={
                'state': state.NODE_STATE_PROVISIONED,
                'nics': [
                    {
                        'ip': instance.private_ip_address,
                    }
                ],
                'metadata': {
                    'ec2_instance_id': instance.id,
                }
            })

        update_spot_instance_request_cache(
            sir_id, metadata=dict(node=node_name, status='fulfilled'))

    def delete_node(self, sir_id):
        with spot_cache:
            cfg = refresh_spot_instance_request_cache()

            if not cfg.has_section(sir_id) or \
                    not cfg.has_option(sir_id, 'node'):
                self.logger.warning(
                    'Spot instance [{0}] does not have an'
                    ' associated node'.format(sir_id))

                return

            spot_instance_node_mapping = cfg.get(sir_id, 'node')

            if spot_instance_node_mapping:
                self.logger.info(
                    'Removing node [{0}] for spot instance'
                    ' request [{1}]'.format(
                        spot_instance_node_mapping, sir_id))

            try:
                NodeApi().deleteNode(spot_instance_node_mapping)
            except NodeNotFound:
                pass


def main():
    parser = optparse.OptionParser()

    aws_group = optparse.OptionGroup(parser, 'AWS Options')

    aws_group.add_option(
        '--region', default='us-east-1',
        help='AWS region to manage Spot Instances in')

    parser.add_option_group(aws_group)

    parser.add_option('--verbose', action='store_true', default=False,
                      help='Enable verbose logging')

    parser.add_option('--daemon', action='store_false',
                      dest='foreground', default=True,
                      help='Start awsspotd in the background')

    parser.add_option('--pidfile', default=PIDFILE,
                      help='Location of PID file')

    polling_group = optparse.OptionGroup(parser, 'Polling Options')

    polling_group.add_option(
        '--polling-interval', '-p', type='int',
        default=SPOT_INSTANCE_POLLING_INTERVAL,
        metavar='<value>',
        help='Polling interval in seconds (default: %default)')

    parser.add_option_group(polling_group)

    options_, args_ = parser.parse_args()

    result_ = [region for region in boto.ec2.regions()
               if region.name == options_.region]
    if not result_:
        sys.stderr.write(
            'Error: Invalid EC2 region [{0}] specified\n'.format(
                options_.region))
        sys.exit(1)

    cls = AWSSpotdAppClass(options_, args_)

    daemon = Daemonize(app='awsspotd', pid=options_.pidfile,
                       action=cls.run,
                       verbose=options_.verbose,
                       foreground=options_.foreground)

    daemon.start()
