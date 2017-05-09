"""
A RESTful API for Ceph
"""

import os
import json
import time
import errno
import inspect
import threading
import traceback

import common

from uuid import uuid4
from pecan import jsonify, make_app
from pecan.rest import RestController
from werkzeug.serving import make_server, make_ssl_devcert

from mgr_module import MgrModule, CommandResult

# Global instance to share
instance = None



class CommandsRequest(object):
    """
    This class handles parallel as well as sequential execution of
    commands. The class accept a list of iterables that should be
    executed sequentially. Each iterable can contain several commands
    that can be executed in parallel.

    Example:
    [[c1,c2],[c3,c4]]
     - run c1 and c2 in parallel
     - wait for them to finish
     - run c3 and c4 in parallel
     - wait for them to finish
    """


    def __init__(self, commands_arrays):
        self.id = str(id(self))

        # Filter out empty sub-requests
        commands_arrays = filter(
            lambda x: len(x) != 0,
            commands_arrays,
        )

        self.running = []
        self.waiting = commands_arrays[1:]
        self.finished = []
        self.failed = []

        self.lock = threading.RLock()
        if not len(commands_arrays):
            # Nothing to run
            return

        # Process first iteration of commands_arrays in parallel
        results = self.run(commands_arrays[0])

        self.running.extend(results)


    def run(self, commands):
        """
        A static method that will execute the given list of commands in
        parallel and will return the list of command results.
        """

        # Gather the results (in parallel)
        results = []
        for index in range(len(commands)):
            tag = '%s:%d' % (str(self.id), index)

            # Store the result
            result = CommandResult(tag)
            result.command = common.humanify_command(commands[index])
            results.append(result)

            # Run the command
            instance.send_command(result, json.dumps(commands[index]), tag)

        return results


    def next(self):
        with self.lock:
            if not self.waiting:
                # Nothing to run
                return

            # Run a next iteration of commands
            commands = self.waiting[0]
            self.waiting = self.waiting[1:]

            self.running.extend(self.run(commands))


    def finish(self, tag):
        with self.lock:
            for index in range(len(self.running)):
                if self.running[index].tag == tag:
                    if self.running[index].r == 0:
                        self.finished.append(self.running.pop(index))
                    else:
                        self.failed.append(self.running.pop(index))
                    return True

            # No such tag found
            return False


    def is_running(self, tag):
        for result in self.running:
            if result.tag == tag:
                return True
        return False


    def is_ready(self):
        with self.lock:
            return not self.running and self.waiting


    def is_waiting(self):
        return bool(self.waiting)


    def is_finished(self):
        with self.lock:
            return not self.running and not self.waiting


    def has_failed(self):
        return bool(self.failed)


    def get_state(self):
        with self.lock:
            if not self.is_finished():
                return "pending"

            if self.has_failed():
                return "failed"

            return "success"


    def __json__(self):
        return {
            'id': self.id,
            'running': map(
                lambda x: (x.command, x.outs, x.outb),
                self.running
            ),
            'finished': map(
                lambda x: (x.command, x.outs, x.outb),
                self.finished
            ),
            'waiting': map(
                lambda x: (x.command, x.outs, x.outb),
                self.waiting
            ),
            'failed': map(
                lambda x: (x.command, x.outs, x.outb),
                self.failed
            ),
            'is_waiting': self.is_waiting(),
            'is_finished': self.is_finished(),
            'has_failed': self.has_failed(),
            'state': self.get_state(),
        }



class Module(MgrModule):
    COMMANDS = [
        {
            "cmd": "create_key name=key_name,type=CephString",
            "desc": "Create an API key with this name",
            "perm": "rw"
        },
        {
            "cmd": "delete_key name=key_name,type=CephString",
            "desc": "Delete an API key with this name",
            "perm": "rw"
        },
        {
            "cmd": "list_keys",
            "desc": "List all API keys",
            "perm": "rw"
        },
    ]

    def __init__(self, *args, **kwargs):
        super(Module, self).__init__(*args, **kwargs)
        global instance
        instance = self

        self.requests = []
        self.requests_lock = threading.RLock()

        self.keys = {}
        self.disable_auth = False

        self.server = None


    def serve(self):
        try:
            self._serve()
        except:
            self.log.error(str(traceback.format_exc()))


    def _serve(self):
        # Load stored authentication keys
        self.keys = self.get_config_json("keys") or {}

        jsonify._instance = jsonify.GenericJSON(
            sort_keys=True,
            indent=4,
            separators=(',', ': '),
        )

        # Create the HTTPS werkzeug server serving pecan app
        self.server = make_server(
            host='0.0.0.0',
            port=8002,
            app=make_app('restful.api.Root'),
            ssl_context=self.load_cert(),
        )

        self.server.serve_forever()


    def shutdown(self):
        try:
            if self.server:
                self.server.shutdown()
        except:
            self.log.error(str(traceback.format_exc()))


    def notify(self, notify_type, tag):
        try:
            self._notify(notify_type, tag)
        except:
            self.log.error(str(traceback.format_exc()))


    def _notify(self, notify_type, tag):
        if notify_type == "command":
            # we can safely skip all the sequential commands
            if tag == 'seq':
                return

            request = filter(
                lambda x: x.is_running(tag),
                self.requests)

            if len(request) != 1:
                self.log.warn("Unknown request '%s'" % str(tag))
                return

            request = request[0]
            request.finish(tag)
            if request.is_ready():
                request.next()
        else:
            self.log.debug("Unhandled notification type '%s'" % notify_type)


    def handle_command(self, command):
        self.log.warn("Handling command: '%s'" % str(command))
        if command['prefix'] == "create_key":
            if command['key_name'] in self.keys:
                return 0, self.keys[command['key_name']], ""

            else:
                self.keys[command['key_name']] = str(uuid4())
                self.set_config_json('keys', self.keys)

            return (
                0,
                self.keys[command['key_name']],
                "",
            )

        elif command['prefix'] == "delete_key":
            if command['key_name'] in self.keys:
                del self.keys[command['key_name']]
                self.set_config_json('keys', self.keys)

            return (
                0,
                "",
                "",
            )

        elif command['prefix'] == "list_keys":
            return (
                0,
                json.dumps(self.get_config_json('keys'), indent=2),
                "",
            )

        else:
            return (
                -errno.EINVAL,
                "",
                "Command not found '{0}'".format(command['prefix'])
            )


    def load_cert(self):
        cert_base = '/etc/ceph/ceph-mgr-restful'
        cert_file = cert_base + '.crt'
        pkey_file = cert_base + '.key'

        # If the files are already there, we are good
        if os.access(cert_file, os.R_OK) and os.access(pkey_file, os.R_OK):
            return (cert_file, pkey_file)

        # If the certificate is in the ceph config db, write it to the files
        cert = self.get_config_json('cert')
        pkey = self.get_config_json('pkey')

        if cert and pkey:
            f = file(cert_file, 'w')
            f.write(cert)
            f.close()

            f = file(pkey_file, 'w')
            f.write(pkey)
            f.close()
            return (cert_file, pkey_file)

        # Otherwise, generate the certificate and save it in the config db
        make_ssl_devcert(cert_base, host='localhost')

        f = file(cert_file, 'r')
        self.set_config_json('cert', f.read())
        f.close()

        f = file(pkey_file, 'r')
        self.set_config_json('pkey', f.read())
        f.close()

        return (cert_file, pkey_file)


    def get_doc_api(self, root, prefix=''):
        doc = {}
        for _obj in dir(root):
            obj = getattr(root, _obj)

            if isinstance(obj, RestController):
                doc.update(self.get_doc_api(obj, prefix + '/' + _obj))

        if getattr(root, '_lookup', None) and isinstance(root._lookup('0')[0], RestController):
            doc.update(self.get_doc_api(root._lookup('0')[0], prefix + '/<arg>'))

        prefix = prefix or '/'

        doc[prefix] = {}
        for method in 'get', 'post', 'patch', 'delete':
            if getattr(root, method, None):
                doc[prefix][method.upper()] = inspect.getdoc(getattr(root, method)).split('\n')

        if len(doc[prefix]) == 0:
            del doc[prefix]

        return doc


    def get_mons(self):
        mon_map_mons = self.get('mon_map')['mons']
        mon_status = json.loads(self.get('mon_status')['json'])

        # Add more information
        for mon in mon_map_mons:
            mon['in_quorum'] = mon['rank'] in mon_status['quorum']
            mon['server'] = self.get_metadata("mon", mon['name'])['hostname']
            mon['leader'] = mon['rank'] == mon_status['quorum'][0]

        return mon_map_mons


    def get_osd_pools(self):
        osds = dict(map(lambda x: (x['osd'], []), self.get('osd_map')['osds']))
        pools = dict(map(lambda x: (x['pool'], x), self.get('osd_map')['pools']))
        crush_rules = self.get('osd_map_crush')['rules']

        osds_by_pool = {}
        for pool_id, pool in pools.items():
            pool_osds = None
            for rule in [r for r in crush_rules if r['ruleset'] == pool['crush_ruleset']]:
                if rule['min_size'] <= pool['size'] <= rule['max_size']:
                    pool_osds = common.crush_rule_osds(self.get('osd_map_tree')['nodes'], rule)

            osds_by_pool[pool_id] = pool_osds

        for pool_id in pools.keys():
            for in_pool_id in osds_by_pool[pool_id]:
                osds[in_pool_id].append(pool_id)

        return osds


    def get_osds(self, pool_id=None, ids=None):
        # Get data
        osd_map = self.get('osd_map')
        osd_metadata = self.get('osd_metadata')

        # Update the data with the additional info from the osd map
        osds = osd_map['osds']

        # Filter by osd ids
        if ids is not None:
            osds = filter(
                lambda x: str(x['osd']) in ids,
                osds
            )

        # Get list of pools per osd node
        pools_map = self.get_osd_pools()

        # map osd IDs to reweight
        reweight_map = dict([
            (x.get('id'), x.get('reweight', None))
            for x in self.get('osd_map_tree')['nodes']
        ])

        # Build OSD data objects
        for osd in osds:
            osd['pools'] = pools_map[osd['osd']]
            osd['server'] = osd_metadata.get(str(osd['osd']), {}).get('hostname', None)

            osd['reweight'] = reweight_map.get(osd['osd'], 0.0)

            if osd['up']:
                osd['valid_commands'] = common.OSD_IMPLEMENTED_COMMANDS
            else:
                osd['valid_commands'] = []

        # Filter by pool
        if pool_id:
            pool_id = int(pool_id)
            osds = filter(
                lambda x: pool_id in x['pools'],
                osds
            )

        return osds


    def get_osd_by_id(self, osd_id):
        osd = filter(
            lambda x: x['osd'] == osd_id,
            self.get('osd_map')['osds']
        )

        if len(osd) != 1:
            return None

        return osd[0]


    def get_pool_by_id(self, pool_id):
        pool = filter(
            lambda x: x['pool'] == pool_id,
            self.get('osd_map')['pools'],
        )

        if len(pool) != 1:
            return None

        return pool[0]


    def submit_request(self, _request, **kwargs):
        request = CommandsRequest(_request)
        with self.requests_lock:
            self.requests.append(request)
        if kwargs.get('wait', 0):
            while not request.is_finished():
                time.sleep(0.001)
        return request


    def run_command(self, command):
        # tag with 'seq' so that we can ingore these in notify function
        result = CommandResult('seq')

        self.send_command(result, json.dumps(command), 'seq')
        return result.wait()
