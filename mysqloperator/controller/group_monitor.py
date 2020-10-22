# Copyright (c) 2020, Oracle and/or its affiliates.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License, version 2.0,
# as published by the Free Software Foundation.
#
# This program is also distributed with certain software (including
# but not limited to OpenSSL) that is licensed under separate terms, as
# designated in a particular file or component or in included license
# documentation.  The authors of MySQL hereby grant you an additional
# permission to link the program and your derivative works with the
# separately licensed software that they have included with MySQL.
# This program is distributed in the hope that it will be useful,  but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See
# the GNU General Public License, version 2.0, for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA

from . import shellutils
import threading
import time
import mysqlsh

mysql = mysqlsh.globals.mysql
mysqlx = mysqlsh.globals.mysqlx

k_connect_retry_interval = 10

class MonitoredCluster:
    def __init__(self, cluster, handler):
        self.cluster = cluster

        self.session = None
        self.target = None
        self.target_not_primary = None
        self.last_connect_attempt = 0
        self.last_primary_id = None
        self.last_view_id = None

        self.handler = handler


    @property
    def name(self):
        return self.cluster.name


    @property
    def namespace(self):
        return self.cluster.namespace


    def ensure_connected(self):
        # TODO run a ping every X seconds
        if not self.session and (not self.last_connect_attempt or time.time() - self.last_connect_attempt > k_connect_retry_interval):
            print(f"GroupMonitor: Trying to connect to a member of cluster {self.cluster.namespace}/{self.cluster.name}")
            self.last_connect_attempt = time.time()
            self.session = None
            self.connect_to_primary()

            # force a refresh after we connect so we don't miss anything
            # that happened while we were out
            if self.session:
                print(f"GroupMonitor: Connect member of {self.cluster.namespace}/{self.cluster.name} OK {self.session}")
                self.on_view_change(None)
            else:
                print(f"GroupMonitor: Connect to member of {self.cluster.namespace}/{self.cluster.name} failed")

        return self.session


    def connect_to_primary(self):
        while True:
            session, is_primary = self.find_primary()
            if not is_primary:
                if session:
                    print(f"GroupMonitor: Could not connect to PRIMARY of cluster {self.cluster.namespace}/{self.cluster.name}")
                else:
                    print(f"GroupMonitor: Could not connect to PRIMARY nor SECONDARY of cluster {self.cluster.namespace}/{self.cluster.name}")

            if session:
                try:
                    # extend number of seconds for the server to wait for a command to arrive to a full day 
                    session.run_sql(f"set session mysqlx_wait_timeout = {24*60*60}")
                    session._enable_notices(["GRViewChanged"])
                    co = shellutils.parse_uri(session.uri)
                    self.target = f"{co['host']}:{co['port']}"
                    self.target_not_primary = not is_primary
                    self.session = session
                except mysqlsh.Error as e:
                    if mysql.ErrorCode.CR_MAX_ERROR >= e.code >= mysql.ErrorCode.CR_MIN_ERROR:
                        # Try again if the server we were connectd to is gone
                        continue
                    else:
                        raise
            else:            
                self.session = None
            break


    def find_primary(self):
        account = self.cluster.get_admin_account()

        not_primary = None

        pods = self.cluster.get_pods()
        # Try to find the PRIMARY the easy way
        for pod in pods:
            member_info = pod.get_membership_info()
            if member_info and member_info.get("role") == "PRIMARY":
                session = self.try_connect(pod)
                if session:
                    s = shellutils.jump_to_primary(session, account)
                    if s:
                        if s != session:
                            session.close()
                        return s, True
                    else:
                        not_primary = session

        # Try to connect to anyone and find the primary from there
        for pod in pods:
            session = self.try_connect(pod)
            if session:
                s = shellutils.jump_to_primary(session, account)
                if s:
                    if s != session:
                        session.close()
                    return s, True
                else:
                    not_primary = session

        return not_primary, False


    def try_connect(self, pod):
        try:
            session = mysqlx.get_session(pod.xendpoint_co)
        except mysqlsh.Error as e:
            print(f"GroupMonitor: Error connecting to {pod.xendpoint}: {e}")
            return None

        return session


    def handle_notice(self):
        while 1:
            try:
                # TODO hack to force unexpected async notice to be read, xsession should read packets itself
                self.session.run_sql("select 1")
                notice = self.session._fetch_notice()
                if not notice:
                    break
                print(f"GOT NOTICE {notice}")
                self.on_view_change(notice.get("view_id"))
                if not self.session:
                    break

            except mysqlsh.Error as e:
                print(f"GroupMonitor: Error fetching notice: dest={self.target} error={e}")
                self.session.close()
                self.session = None
                break


    def on_view_change(self, view_id):
        members = shellutils.query_members(self.session)
        self.handler(self.cluster, members, view_id != self.last_view_id)
        self.last_view_id = view_id

        primary = None
        force_reconnect = False
        for member_id, role, status, view_id, endpoint, version in members:
            if self.last_primary_id == member_id and role != "PRIMARY":
                force_reconnect = True
                break
            if role == "PRIMARY" and not primary:
                primary = member_id

        self.last_primary_id = primary

        # force reconnection if the PRIMARY changed or we're not connected to the PRIMARY
        if self.target_not_primary or force_reconnect:
            print(f"GroupMonitor: PRIMARY changed for {self.cluster.namespace}/{self.cluster.name}")
            if self.session:
                self.session.close()
                self.session = None


# TODO change this to a per cluster kopf.daemon?
class GroupMonitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="group-monitor")

        self.clusters = []
        self.stopped = False


    def monitor_cluster(self, cluster, handler):
        for c in self.clusters:
            if c.name == cluster.name and c.namespace == cluster.namespace:
                return

        target = MonitoredCluster(cluster, handler)
        self.clusters.append(target)
        print(f"Added monitor for {cluster.namespace}/{cluster.name}")


    def remove_cluster(self, cluster):
        for c in self.clusters:
            if c.name == cluster.name and c.namespace == cluster.namespace:
                self.clusters.remove(c)
                break


    def run(self):
        last_ping = time.time()
        while not self.stopped:
            sessions = []
            for cluster in self.clusters:
                cluster.ensure_connected()
                if cluster.session:
                    sessions.append(cluster.session)

            # wait for 1s at most so that newly added session don't wait much
            # TODO replace poll_sessions() with something to get the session fd
            # - do the poll loop in python
            # - add a socket_pair() to allow interrupting the poll when a new
            # cluster is added and increase the timeout
            ready = mysql.poll_sessions(sessions, 1000)
            if ready:
                for i, cluster in enumerate(self.clusters):
                    if cluster.session and cluster.session in ready:
                        cluster.handle_notice()


    def stop(self):
        self.stopped = True


g_group_monitor = GroupMonitor()
