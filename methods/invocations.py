#!/usr/bin/python3
# coding=utf-8

#   Copyright 2025 EPAM Systems
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

""" Method """

import threading

import requests  # pylint: disable=E0401

from pylon.core.tools import log  # pylint: disable=E0611,E0401,W0611
from pylon.core.tools import web  # pylint: disable=E0611,E0401,W0611

from tools import context, this, worker_core  # pylint: disable=E0611,E0401


class Method:  # pylint: disable=E1101,R0903,W0201
    """
        Method Resource

        self is pointing to current Module instance

        web.method decorator takes zero or one argument: method name
        Note: web.method decorator must be the last decorator (at top)
    """

    @web.init()
    def invocation_init(self):
        """ Init """
        self.invocations = {}  # task_id -> invocation key -> data
        self.invocations_lock = threading.Lock()
        #
        worker_core.event_node.subscribe(
            "provider_invocation_started", self.invocation_event_started,
        )
        worker_core.event_node.subscribe(
            "provider_invocation_ended", self.invocation_event_ended,
        )
        #
        indexer_worker = this.for_module("indexer_worker").module
        indexer_worker.agent_event_node.subscribe(
            "task_stop_request", self.invocation_event_task_stop_request,
        )

    @web.deinit()
    def invocation_deinit(self):
        """ De-Init """
        indexer_worker = this.for_module("indexer_worker").module
        indexer_worker.agent_event_node.unsubscribe(
            "task_stop_request", self.invocation_event_task_stop_request,
        )
        #
        worker_core.event_node.unsubscribe(
            "provider_invocation_ended", self.invocation_event_ended,
        )
        worker_core.event_node.unsubscribe(
            "provider_invocation_started", self.invocation_event_started,
        )

    @web.method()
    def invocation_event_started(self, event, data):
        """ Event """
        _ = event
        #
        if data.get("pylon_id") != context.id:
            return
        #
        task_id = data["task_id"]
        invocation_key = self.make_invocation_key(data)
        #
        log.info("Invocation started: %s", invocation_key)
        #
        with self.invocations_lock:
            if task_id not in self.invocations:
                self.invocations[task_id] = {}
            #
            self.invocations[task_id][invocation_key] = data.copy()

    @web.method()
    def invocation_event_ended(self, event, data):
        """ Event """
        _ = event
        #
        if data.get("pylon_id") != context.id:
            return
        #
        task_id = data["task_id"]
        invocation_key = self.make_invocation_key(data)
        #
        log.info("Invocation ended: %s", invocation_key)
        #
        with self.invocations_lock:
            if task_id in self.invocations:
                self.invocations[task_id].pop(invocation_key, None)

    @web.method()
    def invocation_event_task_stop_request(self, event, data):
        """ Event """
        _ = event
        #
        task_id = data.get("task_id")
        #
        with self.invocations_lock:
            if task_id not in self.invocations:
                return
            #
            for invocation_data in self.invocations[task_id].values():
                self.invocation_stop_via_api(invocation_data)

    @web.method()
    def invocation_stop_via_api(self, invocation_data):
        """ Method """
        invocation_key = self.make_invocation_key(invocation_data)
        #
        log.info("Stopping invocation: %s", invocation_key)
        #
        service_location_url = invocation_data["service_location_url"].rstrip("/")
        toolkit_name = invocation_data["toolkit_name"]
        tool_name = invocation_data["tool_name"]
        invocation_id = invocation_data["invocation_id"]
        api_client_kwargs = invocation_data["api_client_kwargs"]
        #
        target_url = "/".join([
            service_location_url,
            "tools",
            toolkit_name,
            tool_name,
            "invocations",
            invocation_id,
        ])
        #
        headers = api_client_kwargs.get("headers", None)
        timeout = api_client_kwargs.get("timeout", 15)
        verify = api_client_kwargs.get("verify", False)
        #
        response = requests.delete(
            target_url,
            headers=headers,
            timeout=timeout,
            verify=verify,
        )
        #
        log.info("Stop invocation response: %s", response)

    @web.method()
    def make_invocation_key(self, data):
        """ Method """
        return "_".join([
            data["provider_name"],
            data["service_location_url"],
            data["toolkit_name"],
            data["tool_name"],
            data["invocation_id"],
        ])
