#!/usr/bin/python

# -*- coding: utf-8 -*-

# Copyright (C) 2009-2012:
#    Gabes Jean, naparuba@gmail.com
#    Gerhard Lausser, Gerhard.Lausser@consol.de
#    Gregory Starck, g.starck@gmail.com
#    Hartmut Goebel, h.goebel@goebel-consult.de
#
# This file is part of Shinken.
#
# Shinken is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Shinken is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Shinken.  If not, see <http://www.gnu.org/licenses/>.

# This Class is an Arbiter module for having a webservice
# where you can push external commands

"""This is a new version of the ws_arbiter. This ws_arbiter supports
multiple external commands.

It can support the following incoming data:
host1, service1, value1, host2, service2, value2

Here is a short command line example:
2 hosts (host1 and host2), one service each (Service1 and Service2)

curl --data "host_name=host1&service_description=Service1
&return_code=0&output=OK+%7C+Service1%3D53%25&time_stamp=1365446900
&host_name=host2&service_description=Service2
&return_code=0&output=OK+%7C+Service2%3D60%25&time_stamp=1365446900" http://shinken_server:7760/push_check_result

It is now possible to do bulk data on the webservice.
"""

import json
import os
import sys
import select
import time

######################## WIP   don't launch it!

from shinken.basemodule import BaseModule
from shinken.external_command import ExternalCommand
from shinken.log import logger

from shinken.webui.bottlewebui import Bottle, run, static_file, view, route, request, response, abort, parse_auth

properties = {
    'daemons': ['arbiter', 'receiver'],
    'type': 'ws_arbiter',
    'external': True,
    }


# called by the plugin manager to get a broker
def get_instance(plugin):
    # logger.info("[WS_Arbiter] get_instance ...")
    instance = Ws_arbiter(plugin)
    return instance

# Main app var. Will be fill with our running module instance
app = None


def check_auth():
    """Check for auth if it's not anonymously allowed"""
    if app.username != 'anonymous':
        basic = parse_auth(request.environ.get('HTTP_AUTHORIZATION', ''))
        # Maybe the user not even ask for user/pass. If so, bail out
        if not basic:
            abort(401, 'Authentication required')
        # Maybe he do not give the good credential?
        if basic[0] != app.username or basic[1] != app.password:
            abort(403, 'Authentication denied')


def get_commands(time_stamps, hosts, services, return_codes, outputs):
    """Composing a command list based on the information received in
    POST request"""

    commands = []

    current_time_stamp = int(time.time())

    def _compose_command(t, h, s, r, o):
        """Simple function to create a command from the inputs"""
        cmd = ""
        if not s or s == "":
            cmd = '[%s] PROCESS_HOST_CHECK_RESULT;%s;%s;%s' % (t if t is not None else current_time_stamp, h, r, o)
        else:
            cmd = '[%s] PROCESS_SERVICE_CHECK_RESULT;%s;%s;%s;%s' % (t if t is not None else current_time_stamp, h, s, r, o)
        logger.debug("[WS_Arbiter] CMD: %s" % (cmd))
        commands.append(cmd)

    # Trivial case: empty commmand list
    if return_codes is None or len(return_codes) == 0:
        return commands

    # Sanity check: if we get N return codes, we must have N hosts.
    # The other values could be None
    if len(return_codes) != len(hosts):
        logger.error("[WS_Arbiter] number of return codes (%d) does not match number of hosts (%d)" % (len(return_codes), len(hosts)))
        abort(400, "number of return codes does not match number of hosts")

    map(_compose_command, time_stamps, hosts, services, return_codes, outputs)
    logger.debug("[WS_Arbiter] received command: %s" % (str(commands)))
    return commands


def parse_do_push_json(request):
    if 'json' in request.get_header('Content-Type', '').lower():
        # Let Bottle do the parsing.
        checks = request.json
    else:
        # Someone forgot to put header "Content-Type: application/json" in its
        # query.
        logger.warning('[WS_Arbiter] Content-Type is not application/json. Trying to parse JSON anyway')
        try:
            checks = json.load(request.body)
        except Exception as err:
            logger.error("[WS_Arbiter] Error while parsing JSON data in push checks: %s" % err)
            abort(400, "An error occured. JSON data malformed.")
    return checks


def parse_push_check_result(request):
    try:
        # Getting lists of informations for the commands
        time_stamp_list = request.forms.getall(key='time_stamp')
        logger.debug("[WS_Arbiter] time_stamp_list: %s" % (time_stamp_list))
        host_name_list = request.forms.getall(key='host_name')
        logger.debug("[WS_Arbiter] host_name_list: %s" % (host_name_list))
        service_description_list = request.forms.getall(key='service_description')
        logger.debug("[WS_Arbiter] service_description_list: %s" % (service_description_list))
        return_code_list = request.forms.getall(key='return_code')
        logger.debug("[WS_Arbiter] return_code_list: %s" % (return_code_list))
        output_list = request.forms.getall(key='output')
        logger.debug("[WS_Arbiter] output_list: %s" % (output_list))
        commands_list = get_commands(time_stamp_list, host_name_list, service_description_list, return_code_list, output_list)
    except Exception as err:
        logger.error("[WS_Arbiter] failed to get the lists: %s" % err)
        commands_list = []

    return commands_list


def do_push_check_result():
    check_auth()

    commands_list = parse_push_check_result(request=request)

    # Adding commands to the main queue()
    logger.debug("[WS_Arbiter] commands: %s" % str(sorted(commands_list)))
    for c in sorted(commands_list):
        ext = ExternalCommand(c)
        app.from_q.put(ext)
    # OK here it's ok, it will return a 200 code


def do_push_checks_perfdata(checks=None):
    check_auth()
    # NB NB NB:
    # see: http://bottlepy.org/docs/0.10/api.html?highlight=json#bottle.BaseRequest.json
    # and http://bottlepy.org/docs/0.10/api.html?highlight=json#bottle.BaseRequest.MEMFILE_MAX
    # so I use:

    if checks is None:
        checks = parse_do_push_json(request=request)

    tnow = time.time()
    for check in checks:
        host_name = check.get('host_name', None)
        if hostname is None:
            logger.warning('check missing host_name key/value ; check_data=%r', check)
            continue

        service_description = check.get('service_description', '')
        if service_description:
            service_description = service_description.strip()

        perfdata = check.get('perfdata', None)
        if perfdata is None:
            logger.warning('check missing output key/value ; check_data=%r', check)
            continue

        check_time = check.get('time', tnow)

        cmd = ExternalCommand(
            '[%d] PROCESS_%s_OUTPUT;%s;%s;WS_Arbiter|%s' % (
                check_time,
                'SERVICE' if service_description else 'HOST',
                host_name,
                service_description,
                perfdata
            )
        )
        app.from_q.put(cmd)


def do_push_check_result_json():
    """
    TODO:
    if return_code:
        call do_push_check_result with output
    else:
        call do_push_checks_perfdata with perfdata
    """
    check_auth()

    checks = parse_do_push_json(request=request)

    for check in checks:
        if check.get('return_code'):
            # We have a return code, let's treat that as a push_check_result
            # TODO: Merge this logic with do_push_check_result()'s
            time_stamp_list = [check.get('time_stamp')]
            host_name_list = [check.get('host_name')]
            service_description_list = [check.get('service_description')]
            return_code_list = [check.get('return_code')]
            output_list = [check.get('output')]

            ext = ExternalCommand(
                get_commands(
                    time_stamp_list,
                    host_name_list,
                    service_description_list,
                    return_code_list,
                    output_list
                )
            )
            app.from_q.put(ext)
        else:
            # We don't have a return code, let's treat that as a
            # push_checks_perfdata.
            # do_push_checks_perfdata() will add the ExternalCommand to the
            # app.from_q
            do_push_checks_perfdata([check])


def do_restart():
    check_auth()

    # Getting lists of informations for the commands
    time_stamp = request.forms.get('time_stamp', int(time.time()))
    command = '[%s] RESTART_PROGRAM\n' % time_stamp

    # Adding commands to the main queue()
    logger.warning("[WS_Arbiter] command: %s" % str(command))
    ext = ExternalCommand(command)
    app.from_q.put(ext)

    # OK here it's ok, it will return a 200 code


def do_reload():
    check_auth()

    # Getting lists of informations for the commands
    time_stamp = request.forms.get('time_stamp', int(time.time()))
    command = '[%s] RELOAD_CONFIG\n' % time_stamp

    # Adding commands to the main queue()
    logger.warning("[WS_Arbiter] command: %s" % str(command))
    ext = ExternalCommand(command)
    app.from_q.put(ext)

    # OK here it's ok, it will return a 200 code


#service, sticky, notify, persistent, author, comment
def do_acknowledge():
    check_auth()

    # Getting lists of informations for the commands
    action              = request.forms.get('action', 'add')
    time_stamp          = request.forms.get('time_stamp', int(time.time()))
    host_name           = request.forms.get('host_name', '')
    service_description = request.forms.get('service_description', '')
    sticky              = request.forms.get('sticky', '1')
    notify              = request.forms.get('notify', '0')
    persistent          = request.forms.get('persistent', '1')
    author              = request.forms.get('author', 'anonymous')
    comment             = request.forms.get('comment', 'No comment')
    logger.debug("[WS_Arbiter] Acknowledge %s - host: '%s', service: '%s', comment: '%s'" % (action, host_name, service_description, comment))

    if not host_name:
        abort(400, 'Missing parameter host_name')

    if action == 'add':
        if service_description:
            command = '[%s] ACKNOWLEDGE_SVC_PROBLEM;%s;%s;%s;%s;%s;%s;%s\n' % ( time_stamp,
                                                                                host_name,
                                                                                service_description,
                                                                                sticky,
                                                                                notify,
                                                                                persistent,
                                                                                author,
                                                                                comment
                                                                                )
        else:
            command = '[%s] ACKNOWLEDGE_HOST_PROBLEM;%s;%s;%s;%s;%s;%s\n' % (   time_stamp,
                                                                                host_name,
                                                                                sticky,
                                                                                notify,
                                                                                persistent,
                                                                                author,
                                                                                comment
                                                                                )

    if action == 'delete':
        if service_description:
            # REMOVE_SVC_ACKNOWLEDGEMENT;<host_name>;<service_description>
            command = '[%s] REMOVE_SVC_ACKNOWLEDGEMENT;%s;%s\n' % ( time_stamp,
                                                                    host_name,
                                                                    service_description)
        else:
            # REMOVE_HOST_ACKNOWLEDGEMENT;<host_name>
            command = '[%s] REMOVE_HOST_ACKNOWLEDGEMENT;%s\n' % ( time_stamp,
                                                                  host_name)


    # logger.warning("[WS_Arbiter] command: %s" % (command))

    # Adding commands to the main queue()
    logger.debug("[WS_Arbiter] command: %s" % str(command))
    ext = ExternalCommand(command)
    app.from_q.put(ext)

    # OK here it's ok, it will return a 200 code


def do_recheck():
    # We check for auth if it's not anonymously allowed
    check_auth()

    # Getting lists of informations for the commands
    time_stamp          = request.forms.get('time_stamp', int(time.time()))
    host_name           = request.forms.get('host_name', '')
    service_description = request.forms.get('service_description', '')
    logger.debug("[WS_Arbiter] Timestamp '%s' - host: '%s', service: '%s'" % (time_stamp,
                                                                              host_name,
                                                                              service_description
                                                                             )
                )

    if not host_name:
        abort(400, 'Missing parameter host_name')

    if service_description:
        # SCHEDULE_FORCED_SVC_CHECK;<host_name>;<service_description>;<check_time>
        command = '[%s] SCHEDULE_FORCED_SVC_CHECK;%s;%s;%s\n' % (time_stamp,
                                                                 host_name,
                                                                 service_description,
                                                                 time_stamp)
    else:
        # SCHEDULE_FORCED_HOST_CHECK;<host_name>;<check_time>
        command = '[%s] SCHEDULE_FORCED_HOST_CHECK;%s;%s\n' % (time_stamp,
                                                               host_name,
                                                               time_stamp)

    # Adding commands to the main queue()
    logger.debug("[WS_Arbiter] command =  %s" % command)
    ext = ExternalCommand(command)
    app.from_q.put(ext)

    # OK here it's ok, it will return a 200 code


def do_downtime():
    check_auth()

    # Getting lists of informations for the commands
    action              = request.forms.get('action', 'add')
    time_stamp          = request.forms.get('time_stamp', int(time.time()))
    host_name           = request.forms.get('host_name', '')
    service_description = request.forms.get('service_description', '')
    start_time          = request.forms.get('start_time', int(time.time()))
    end_time            = request.forms.get('end_time', int(time.time()))
    # Fixed is 1 for a period between start and end time
    fixed               = request.forms.get('fixed', '1')
    # Fixed is 0 (flexible) for a period of duration seconds from start time
    duration            = request.forms.get('duration', int('86400'))
    trigger_id          = request.forms.get('trigger_id', '0')
    author              = request.forms.get('author', 'anonymous')
    comment             = request.forms.get('comment', 'No comment')
    logger.debug("[WS_Arbiter] Downtime %s - host: '%s', service: '%s', comment: '%s'" % (action, host_name, service_description, comment))

    if not host_name:
        abort(400, 'Missing parameter host_name')

    if action == 'add':
        if service_description:
            # SCHEDULE_SVC_DOWNTIME;<host_name>;<service_description>;<start_time>;<end_time>;<fixed>;<trigger_id>;<duration>;<author>;<comment>
            command = '[%s] SCHEDULE_SVC_DOWNTIME;%s;%s;%s;%s;%s;%s;%s;%s;%s\n' % ( time_stamp,
                                                                                    host_name,
                                                                                    service_description,
                                                                                    start_time,
                                                                                    end_time,
                                                                                    fixed,
                                                                                    trigger_id,
                                                                                    duration,
                                                                                    author,
                                                                                    comment
                                                                                   )
        else:
            # SCHEDULE_HOST_DOWNTIME;<host_name>;<start_time>;<end_time>;<fixed>;<trigger_id>;<duration>;<author>;<comment>
            command = '[%s] SCHEDULE_HOST_DOWNTIME;%s;%s;%s;%s;%s;%s;%s;%s\n' % (   time_stamp,
                                                                                    host_name,
                                                                                    start_time,
                                                                                    end_time,
                                                                                    fixed,
                                                                                    trigger_id,
                                                                                    duration,
                                                                                    author,
                                                                                    comment
                                                                                )

    if action == 'delete':
        if service_description:
            # DEL_ALL_SVC_DOWNTIMES;<host_name>;<service_description>
            command = '[%s] DEL_ALL_SVC_DOWNTIMES;%s;%s\n' % ( time_stamp,
                                                               host_name,
                                                               service_description)
        else:
            # DEL_ALL_SVC_DOWNTIMES;<host_name>
            command = '[%s] DEL_ALL_HOST_DOWNTIMES;%s\n' % ( time_stamp,
                                                             host_name)

    # Adding commands to the main queue()
    logger.debug("[WS_Arbiter] command =  %s" % command)
    ext = ExternalCommand(command)
    app.from_q.put(ext)

    # OK here it's ok, it will return a 200 code


# This module will open an HTTP service, where a user can send a command, like a check
# return.
class Ws_arbiter(BaseModule):
    def __init__(self, modconf):
        BaseModule.__init__(self, modconf)
        try:
            logger.debug("[WS_Arbiter] Configuration starting ...")
            self.username = getattr(modconf, 'username', 'anonymous')
            self.password = getattr(modconf, 'password', '')
            self.port = int(getattr(modconf, 'port', '7760'))
            self.host = getattr(modconf, 'host', '0.0.0.0')

            self.routes = getattr(modconf, 'routes', None)
            if self.routes is not None:
                self.routes = self.routes.split(',')

            logger.info("[WS_Arbiter] Configuration done, host: %s(%s), username: %s)" %(self.host, self.port, self.username))
        except AttributeError:
            logger.error("[WS_Arbiter] The module is missing a property, check module declaration in shinken-specific.cfg")
            raise
        except Exception as err:
            logger.error("[WS_Arbiter] Exception : %s" % err)
            raise

    # We initialize the HTTP part. It's a simple wsgi backend
    # with a select hack so we can still exit if someone ask it
    def init_http(self):
        logger.info("[WS_Arbiter] Starting WS arbiter http socket")
        try:
            self.srv = run(host=self.host, port=self.port, server='wsgirefselect')
        except Exception as err:
            logger.error("[WS_Arbiter] Exception : %s" % err)
            raise

        logger.info("[WS_Arbiter] Server started")
        # And we link our page

        if self.routes is None or 'push_check_result' in self.routes:
            route('/push_check_result', callback=do_push_check_result, method='POST')

        if self.routes is None or 'restart' in self.routes:
            route('/restart', callback=do_restart, method='POST')

        if self.routes is None or 'reload' in self.routes:
            route('/reload', callback=do_reload, method='POST')

        if self.routes is None or 'acknowledge' in self.routes:
            route('/acknowledge', callback=do_acknowledge, method='POST')

        if self.routes is None or 'downtime' in self.routes:
            route('/downtime', callback=do_downtime, method='POST')

        if self.routes is None or 'recheck' in self.routes:
            route('/recheck', callback=do_recheck, method='POST')

        if self.routes is None or 'push_checks_perfdata' in self.routes:
            route('/push_checks_perfdata', callback=do_push_checks_perfdata,
                  method='POST')

        if self.routes is None or 'push_check_result_json' in self.routes:
            route('/push_check_result_jfon',
                  callback=do_push_check_result_json,
                  method='POST')

    # When you are in "external" mode, that is the main loop of your process
    def main(self):
        global app

        # Change process name (seen in ps or top)
        self.set_proctitle(self.name)

        # It's an external module, so we need to be sure that we manage
        # the signals
        self.set_exit_handler()

        # Go for Http open :)
        self.init_http()

        # We fill the global variable with our Queue() link
        # with the arbiter, because the page should be a non-class
        # one function
        app = self

        # We will loop forever on the http socket
        input = [self.srv.socket]

        # Main blocking loop
        while not self.interrupted:
            input = [self.srv.socket]
            try:
                inputready, _, _ = select.select(input, [], [], 1)
            except select.error as err:
                logger.warning("[WS_Arbiter] Exception: %s", err)
                continue
            for s in inputready:
                # If it's a web request, ask the webserver to do it
                if s == self.srv.socket:
                    self.srv.handle_request()
