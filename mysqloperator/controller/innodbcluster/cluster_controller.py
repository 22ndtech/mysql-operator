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
 

from .. import consts, errors, kubeutils, shellutils, utils, config, mysqlutils
from .. import diagnose
from . import router_objects
from .cluster_api import MySQLPod, InnoDBCluster, client
import os
import copy
import mysqlsh
import kopf
import datetime
import time

common_gr_options = {
    # Abort the server if member is kicked out of the group, which would trigger
    # an event from the container restart, which we can catch and act upon.
    # This also makes autoRejoinTries irrelevant.
    "exitStateAction": "ABORT_SERVER"
}


class ClusterMutex:
    def __init__(self, cluster, pod = None):
        self.cluster = cluster
        self.pod = pod


    def __enter__(self, *args):
        owner = utils.g_ephemeral_pod_state.testset(self.cluster, "cluster-mutex", self.pod.name if self.pod else self.cluster.name)
        if owner:
            print(f"FAILED LOCK FOR {self.pod or self.cluster.name}")
            raise kopf.TemporaryError(f"{self.cluster.name} busy.  lock_owner={owner}", delay=10)
        print(f"ACQUIRED LOCK FOR {self.pod or self.cluster.name}")

    def __exit__(self, *args):
        print(f"RELEASED LOCK FOR {self.pod or self.cluster.name}")
        utils.g_ephemeral_pod_state.set(self.cluster, "cluster-mutex", None)


class ClusterController:
    """
    This is the controller for a innodbcluster object.
    It's the main controller for a cluster and drives the lifecycle of the
    cluster including creation, scaling and restoring from outages.
    """

    def __init__(self, cluster):
        self.cluster=cluster
        self.dba=None
        self.dba_cluster=None


    def publish_status(self, diag):
        cluster_status = {
            "status": diag.status,
            "onlineInstances": len(diag.online_members),
            "lastProbeTime": utils.isotime()
        }
        self.cluster.set_cluster_status(cluster_status)


    def probe_status(self, logger):
        diag = diagnose.diagnose_cluster(self.cluster, logger)
        self.publish_status(diag)
        logger.info(f"cluster probe: status={diag.status} online={diag.online_members}")
        return diag


    def probe_status_if_needed(self, changed_pod, logger):
        cluster_probe_time = self.cluster.get_cluster_status("lastProbeTime") 
        member_transition_time = changed_pod.get_membership_info("lastTransitionTime")
        last_status = self.cluster.get_cluster_status("status")
        unreachable_states = (diagnose.DIAG_CLUSTER_UNKNOWN, diagnose.DIAG_CLUSTER_ONLINE_UNCERTAIN, diagnose.DIAG_CLUSTER_OFFLINE_UNCERTAIN, diagnose.DIAG_CLUSTER_NO_QUORUM_UNCERTAIN, diagnose.DIAG_CLUSTER_SPLIT_BRAIN_UNCERTAIN)
        if cluster_probe_time and member_transition_time and cluster_probe_time < member_transition_time or last_status in unreachable_states:
            return self.probe_status(logger).status
        else:
            return last_status


    def probe_member_status(self, pod, session, joined, logger):
        # TODO use diagnose?
        member_id, role, status, view_id, version = shellutils.query_membership_info(session)
        logger.debug(f"instance probe: role={role} status={status} view_id={view_id} version={version}")
        pod.update_membership_status(member_id, role, status, view_id, version, joined=joined)
         # TODO
        if status == "ONLINE":
            pod.update_member_readiness_gate("ready", True)
        else:
            pod.update_member_readiness_gate("ready", False)

    def connect_to_primary(self, primary_pod, logger):
        if primary_pod:
            self.dba = shellutils.connect_dba(primary_pod.endpoint_co, logger, max_tries=2)
            self.dba_cluster = self.dba.get_cluster()
        else:
            # - check if we should consider pod marker for whether the instance joined
            self.connect_to_cluster(logger)
        return self.dba_cluster


    def connect_to_cluster(self, logger):
        # Get list of pods and try to connect to one of them
        def try_connect():
            last_exc = None
            offline_pods = []
            all_pods = self.cluster.get_pods()
            for pod in all_pods:
                if pod.name in offline_pods or pod.deleting:
                    continue

                try:
                    self.dba = mysqlsh.create_dba(pod.endpoint_co)
                except Exception as e:
                    logger.debug(f"create_dba: target={pod.name} error={e}")
                    # Try another pod if we can't connect to it
                    last_exc = e
                    continue

                try:
                    self.dba_cluster = self.dba.get_cluster()
                    # TODO check whether member is ONLINE/quorum
                    status = self.dba_cluster.status()
                    logger.info(f"Connected to {pod} - {status}")
                    return pod
                except mysqlsh.Error as e:
                    logger.info(
                        f"get_cluster() from {pod.name} failed: {e}")

                    if e.code == errors.SHERR_DBA_BADARG_INSTANCE_NOT_ONLINE:
                        # This member is not ONLINE, so there's no chance of 
                        # getting a cluster handle from it
                        offline_pods.append(pod.name)

                except Exception as e:
                    logger.info(
                        f"get_cluster() from {pod.name} failed: {e}")

            # If all pods are connectable but OFFLINE, then we have complete outage and need a reboot
            if len(offline_pods) == len(all_pods):
                raise kopf.TemporaryError("Could not connect to any cluster member", delay=15)

            if last_exc:
                raise last_exc

            raise kopf.TemporaryError("Could not connect to any cluster member", delay=15)

        return try_connect()


    def log_mysql_info(self, pod, session, logger):
        row = session.run_sql("select @@server_id, @@server_uuid, @@report_host").fetch_one()
        server_id, server_uuid, report_host = row
        try:
            row = session.run_sql("select @@globals.gtid_executed, @@globals.gtid_purged").fetch_one()
            gtid_executed, gtid_purged = row
        except:
            gtid_executed, gtid_purged = None, None

        logger.info(f"server_id={server_id} server_uuid={server_uuid}  report_host={report_host}  gtid_executed={gtid_executed}  gtid_purged={gtid_purged}")


    def create_cluster(self, seed_pod, logger):
        logger.info("Creating cluster at %s" % seed_pod.name)

        assume_gtid_set_complete = False
        if self.cluster.parsed_spec.initDB:
            # TODO store version 
            # TODO store last known quorum
            if self.cluster.parsed_spec.initDB.clone:
                self.cluster.update_cluster_info({
                    "initialDataSource": f"clone={self.cluster.parsed_spec.initDB.clone.uri}",
                    "incrementalRecoveryAllowed": False
                })
            else:
                assert 0, "internal error"
        else:
            # We're creating the cluster from scratch, so GTID set is sure to be complete
            assume_gtid_set_complete = True

            self.cluster.update_cluster_info({
                "initialDataSource": "blank",
                "incrementalRecoveryAllowed": True
            })

        # The operator manages GR, so turn off start_on_boot to avoid conflicts
        create_options = {
            "gtidSetIsComplete": assume_gtid_set_complete,
            "startOnBoot": False,
            "memberSslMode": "REQUIRED"
        }
        create_options.update(common_gr_options)

        def should_retry(err):
            if seed_pod.deleting:
                return False
            return True

        with shellutils.connect_dba(seed_pod.endpoint_co, logger, is_retriable=should_retry) as dba:
            try:
                self.dba_cluster = dba.get_cluster()
                # maybe from a previous incomplete create attempt
                logger.info("Cluster already exists")
            except:
                self.dba_cluster = None

            seed_pod.add_member_finalizer()

            if not self.dba_cluster:
                self.log_mysql_info(seed_pod, dba.session, logger)

                logger.info(
                    f"create_cluster: seed={seed_pod.name}, options={create_options}")

                try:
                    self.dba_cluster = dba.create_cluster(self.cluster.name, create_options)

                    logger.info("create_cluster OK")
                except mysqlsh.Error as e:
                    # If creating the cluster failed, remove the membership finalizer
                    seed_pod.remove_member_finalizer()

                    # can happen when retrying
                    if e.code == errors.SHERR_DBA_BADARG_INSTANCE_ALREADY_IN_GR:
                        logger.info(f"GR already running at {seed_pod.endpoint}, stopping before retrying...")

                        try:
                            dba.session.run_sql("STOP GROUP_REPLICATION")
                        except mysqlsh.Error as e:
                            logger.info(f"Could not stop GR plugin: {e}")
                            # throw a temporary error for a full retry later
                            raise kopf.TemporaryError("GR already running while creating cluster but could not stop it", delay=3)
                    raise

            self.probe_member_status(seed_pod, dba.session, True, logger)

            logger.debug("Cluster created %s" % self.dba_cluster.status())

            self.post_create_actions(dba, self.dba_cluster, seed_pod, logger)


    def post_create_actions(self, dba, dba_cluster, seed_pod, logger):
        # create router account
        user, password = self.cluster.get_router_account()

        update = True
        try:
            dba.session.run_sql("show grants for ?@'%'", [user])
        except mysqlsh.Error as e:
            if e.code == mysqlsh.globals.mysql.ErrorCode.ER_NONEXISTING_GRANT:
                update = False
            else:
                raise
        logger.debug(f"{'Updating' if update else 'Creating'} router account {user}")
        dba_cluster.setup_router_account(user, {"password": password, "update": update})

        # create backup account
        user, password = self.cluster.get_backup_account()
        logger.debug(f"Creating backup account {user}")
        mysqlutils.setup_backup_account(dba.session, user, password)

        # update the router replicaset
        n = self.cluster.spec.get("routers")
        if n:
            logger.debug(f"Setting router replicas to {n}")
            router_objects.update_size(self.cluster, n, logger)


    def reboot_cluster(self, logger):
        logger.info(f"Rebooting cluster {self.cluster.name}...")
        
        # Reboot from cluster-0
        # TODO check if we need to find the member with the most GTIDs first
        pods = self.cluster.get_pods()
        seed_pod = pods[0]

        with shellutils.connect_dba(seed_pod.endpoint_co, logger) as dba:
            logger.info(f"reboot_cluster_from_complete_outage: seed={seed_pod}")
            self.log_mysql_info(seed_pod, dba.session, logger)

            seed_pod.add_member_finalizer()

            cluster = dba.reboot_cluster_from_complete_outage()

            logger.info(f"reboot_cluster_from_complete_outage OK.")

            # Rejoin everyone
            #for pod in pods[1:]:
            #    self.reconcile_pod(seed_pod, pod, logger)

            status = cluster.status()
            logger.info(f"Cluster reboot successful. status={status}")


    def force_quorum(self, seed_pod, logger):
        logger.info(f"Forcing quorum of cluster {self.cluster.name} using {seed_pod.name}...")

        self.connect_to_primary(seed_pod, logger)

        self.dba_cluster.force_quorum_using_partition_of(seed_pod.endpoint_co)

        status = self.dba_cluster.status()
        logger.info(f"Force quorum successful. status={status}")

        # TODO Rejoin OFFLINE members


    def destroy_cluster(self, last_pod, logger):
        logger.info(f"Stopping GR for last cluster member {last_pod.name}")

        try:
            with shellutils.connect_to_pod(last_pod, logger, timeout=5) as session:
                # Just stop GR
                session.run_sql("STOP group_replication")
        except Exception as e:
            logger.warning(f"Error stopping GR at last cluster member, ignoring... {e}")
            # Remove the pod membership finalizer even if we couldn't do final cleanup
            # (it's just stop GR, which should be harmless most of the time)
            last_pod.remove_member_finalizer()
            return

        logger.info("Stop GR OK")

        last_pod.remove_member_finalizer()


    def reconcile_pod(self, primary_pod: MySQLPod, pod: MySQLPod, logger):
        with shellutils.connect_dba(pod.endpoint_co, logger) as pod_dba_session:
            cluster = self.connect_to_primary(primary_pod, logger)

            status = diagnose.diagnose_cluster_candidate(self.dba.session, cluster, pod, pod_dba_session, logger)

            logger.info(f"Reconciling {pod}: state={status.status}  deleting={pod.deleting} cluster_deleting={self.cluster.deleting}")
            if pod.deleting or self.cluster.deleting:
                return

            # TODO check case where a member pod was deleted and then rejoins with the same address but different uuid

            if status.status == diagnose.DIAG_CANDIDATE_JOINABLE:
                self.join_instance(pod, logger, pod_dba_session)

            elif status.status == diagnose.DIAG_CANDIDATE_REJOINABLE:
                self.rejoin_instance(pod, logger, pod_dba_session)

            elif status.status == diagnose.DIAG_CANDIDATE_MEMBER:
                logger.info(f"{pod.endpoint} already a member")

            elif status.status == diagnose.DIAG_CANDIDATE_UNREACHABLE:
                # TODO check if we should throw a tmp error or do nothing
                logger.error(f"{pod.endpoint} is unreachable")

            else:
                # TODO check if we can repair broken instances
                # It would be possible to auto-repair an instance with errant
                # transactions by cloning over it, but that would mean these
                # errants are lost.
                logger.error(f"{pod.endpoint} is in state {status.status}")


    def join_instance(self, pod, logger, pod_dba_session):
        logger.info(f"Adding {pod.endpoint} to cluster")

        peer_pod = self.connect_to_cluster(logger)

        self.log_mysql_info(pod, pod_dba_session.session, logger)

        recovery_method = "clone"
        # TODO - always use clone when dataset is big
        if self.cluster.incremental_recovery_allowed():
            recovery_method = "incremental"

        add_options = {"recoveryMethod": recovery_method}
        add_options.update(common_gr_options)

        logger.info(
            f"add_instance: target={pod.endpoint}  cluster_peer={peer_pod.endpoint}  options={add_options}...")

        try:
            pod.add_member_finalizer()

            self.dba_cluster.add_instance(pod.endpoint_co, add_options)

            logger.debug("add_instance OK")
        except mysqlsh.Error as e:
            logger.warning(f"add_instance failed: error={e}")

            raise

        self.probe_member_status(pod, pod_dba_session.session, True, logger)


    def rejoin_instance(self, pod, logger, pod_dba_session):
        logger.info(f"Rejoining {pod.endpoint} to cluster")

        peer_pod = self.connect_to_cluster(logger)

        self.log_mysql_info(pod, pod_dba_session.session, logger)

        rejoin_options = {}

        logger.info(
            f"rejoin_instance: target={pod.endpoint}  cluster_peer={peer_pod.endpoint}  options={rejoin_options}...")

        try:
            self.dba_cluster.rejoin_instance(pod.endpoint, rejoin_options)

            logger.debug("rejoin_instance OK")
        except mysqlsh.Error as e:
            logger.warning(f"rejoin_instance failed: error={e}")
            raise
    
        self.probe_member_status(pod, pod_dba_session.session, False, logger)


    def remove_instance(self, pod, pod_body, logger, force = False):
        logger.info(f"Removing {pod.endpoint} from cluster")

        # TODO improve this check
        if len(self.cluster.get_pods()) > 1:
            try:
                peer_pod = self.connect_to_cluster(logger)
            except mysqlsh.Error as e:
                peer_pod = None
                if self.cluster.deleting:
                    logger.warning(f"Could not connect to cluster, but ignoring because we're deleting: error={e}")
                else:
                    logger.error(f"Could not connect to cluster: error={e}")
                    raise

            if peer_pod:
                removed = False
                if not force:
                    remove_options = {}
                    logger.info(f"remove_instance: {pod.name}  peer={peer_pod.name}  options={remove_options}")
                    try:
                        self.dba_cluster.remove_instance(pod.endpoint, remove_options)
                        removed = True
                        logger.debug("remove_instance OK")
                    except mysqlsh.Error as e:
                        logger.warning(f"remove_instance failed: error={e}")
                        if e.code == mysqlsh.globals.mysql.ErrorCode.ER_OPTION_PREVENTS_STATEMENT:
                            # super_read_only can still be true on a PRIMARY for a
                            # short time
                            raise kopf.TemporaryError(f"{peer_pod.name} is a PRIMARY but super_read_only is ON", delay=5)
                        elif e.code == errors.SHERR_DBA_MEMBER_METADATA_MISSING:
                            # already removed and we're probably just retrying
                            removed = True

                if not removed:
                    # Try with force
                    remove_options = {"force":True}

                    logger.info(f"remove_instance: {pod.name}  peer={peer_pod.name}  options={remove_options}")
                    try:
                        self.dba_cluster.remove_instance(pod.endpoint, remove_options)

                        logger.info("remove_instance OK")
                    except mysqlsh.Error as e:
                        logger.warning(f"remove_instance failed: error={e}")
                        if e.code == errors.SHERR_DBA_MEMBER_METADATA_MISSING:
                            pass
                        else:
                            deleting = not self.cluster or self.cluster.deleting
                            if deleting:
                                logger.info(f"force remove_instance failed. Ignoring because cluster is deleted: error={e}  peer={peer_pod.name}")
                            else:
                                logger.error(f"force remove_instance failed. error={e} deleting_cluster={deleting}  peer={peer_pod.name}")
                                raise                
            else:
                logger.error(f"Cluster is not available, skipping clean removal of {pod.name}")

        # Remove the membership finalizer to allow the pod to be removed
        pod.remove_member_finalizer(pod_body)
        logger.info(f"Removed finalizer for pod {pod_body['metadata']}")


    def repair_cluster(self, pod, diagnostic, logger):
        # TODO check statuses where router has to be put down

        # Restore cluster to an ONLINE state
        if diagnostic.status == diagnose.DIAG_CLUSTER_ONLINE:
            # Nothing to do
            return

        elif diagnostic.status == diagnose.DIAG_CLUSTER_ONLINE_PARTIAL:
            # Nothing to do, rejoins handled on pod events
            return

        elif diagnostic.status == diagnose.DIAG_CLUSTER_ONLINE_UNCERTAIN:
            # Nothing to do
            # TODO maybe delete unreachable pods if enabled?
            return

        elif diagnostic.status == diagnose.DIAG_CLUSTER_OFFLINE:
            # Reboot cluster if this is pod-0
            if pod.index == 0:
                logger.info(f"Rebooting cluster in state {diagnostic.status}...")
                shellutils.RetryLoop(logger).call(self.reboot_cluster, logger)
            else:
                logger.info(f"Cluster in state {diagnostic.status}")

        elif diagnostic.status == diagnose.DIAG_CLUSTER_OFFLINE_UNCERTAIN:
            # TODO delete unconnectable pods after timeout, if enabled
            raise kopf.TemporaryError(f"Unreachable members found while in state {diagnostic.status}, waiting...")

        elif diagnostic.status == diagnose.DIAG_CLUSTER_NO_QUORUM:
            # Restore cluster
            logger.info(f"Forcing quorum on cluster in state {diagnostic.status}...")
            shellutils.RetryLoop(logger).call(self.force_quorum, diagnostic.quorum_candidates[0], logger)

        elif diagnostic.status == diagnose.DIAG_CLUSTER_NO_QUORUM_UNCERTAIN:
            # Restore cluster
            # TODO delete unconnectable pods after timeout, if enabled
            raise kopf.TemporaryError(f"Unreachable members found while in state {diagnostic.status}, waiting...")
        
        elif diagnostic.status == diagnose.DIAG_CLUSTER_SPLIT_BRAIN:
            # TODO check if recoverable case
            # Fatal error, user intervention required
            raise kopf.PermanentError(f"Unable to recover from current cluster state. User action required. state={diagnostic.status}")

        elif diagnostic.status == diagnose.DIAG_CLUSTER_SPLIT_BRAIN_UNCERTAIN:
            # TODO check if recoverable case and if NOT, then throw a permanent error
            raise kopf.PermanentError(f"Unable to recover from current cluster state. User action required. state={diagnostic.status}")
            # TODO delete unconnectable pods after timeout, if enabled
            raise kopf.TemporaryError(f"Unreachable members found while in state {diagnostic.status}, waiting...")

        elif diagnostic.status == diagnose.DIAG_CLUSTER_UNKNOWN:
            # Nothing to do, but we can try again later and hope something comes back
            raise kopf.TemporaryError(f"No members of the cluster could be reached. state={diagnostic.status}")

        elif diagnostic.status == diagnose.DIAG_CLUSTER_INVALID:
            raise kopf.PermanentError(f"Unable to recover from current cluster state. User action required. state={diagnostic.status}")

        elif diagnostic.status == diagnose.DIAG_CLUSTER_FINALIZING:
            # Nothing to do
            return

        else:
            raise kopf.PermanentError(f"Invalid cluster state {diagnostic.status}")


    def on_pod_created(self, pod, logger):
        diag = self.probe_status(logger)

        logger.debug(f"on_pod_created: pod={pod.name} primary={diag.primary} cluster_state={diag.status}")

        if diag.status == diagnose.DIAG_CLUSTER_INITIALIZING:
            # If cluster is not yet created, then we create it at pod-0
            if pod.index == 0:
                if self.cluster.get_create_time():
                    raise kopf.PermanentError(f"Internal inconsistency: cluster marked as initialized, but create requested again")

                shellutils.RetryLoop(logger).call(self.create_cluster, pod, logger)

                # Mark the cluster object as already created
                self.cluster.set_create_time(datetime.datetime.now())
            else:
                # Other pods must wait for the cluster to be ready
                raise kopf.TemporaryError("Cluster is not yet ready", delay=15)

        elif diag.status in (diagnose.DIAG_CLUSTER_ONLINE, diagnose.DIAG_CLUSTER_ONLINE_PARTIAL, diagnose.DIAG_CLUSTER_ONLINE_UNCERTAIN):
            # Cluster exists and is healthy, join the pod to it
            shellutils.RetryLoop(logger).call(self.reconcile_pod, diag.primary, pod, logger)
        else:
            self.repair_cluster(pod, diag, logger)

            # Retry from scratch in another iteration
            raise kopf.TemporaryError(f"Cluster repair from state {diag.status} attempted", delay=3)


    def on_pod_restarted(self, pod, logger):
        diag = self.probe_status(logger)

        logger.debug(f"on_pod_restarted: pod={pod.name}  primary={diag.primary}  cluster_state={diag.status}")

        if diag.status not in (diagnose.DIAG_CLUSTER_ONLINE, diagnose.DIAG_CLUSTER_ONLINE_PARTIAL):
            self.repair_cluster(pod, diag, logger)

        shellutils.RetryLoop(logger).call(self.reconcile_pod, diag.primary, pod, logger)

    
    def on_pod_deleted(self, pod, pod_body, logger):
        diag = self.probe_status(logger)

        logger.debug(f"on_pod_deleted: pod={pod.name}  primary={diag.primary}  cluster_state={diag.status}")

        if self.cluster.deleting:
            # cluster is being deleted, if this is pod-0 shut it down
            if pod.index == 0:
                self.destroy_cluster(pod, logger)
                pod.remove_member_finalizer(pod_body)
                return

        if pod.deleting or diag.status in (diagnose.DIAG_CLUSTER_ONLINE, diagnose.DIAG_CLUSTER_ONLINE_PARTIAL, diagnose.DIAG_CLUSTER_ONLINE_UNCERTAIN, diagnose.DIAG_CLUSTER_FINALIZING):
            shellutils.RetryLoop(logger).call(self.remove_instance, pod, pod_body, logger)
        else:
            if self.repair_cluster(pod, diag, logger):
                # Retry from scratch in another iteration
                raise kopf.TemporaryError(f"Cluster repair from state {diag.status} attempted", delay=3)

        # TODO maybe not needed? need to make sure that shrinking cluster will be reported as ONLINE
        self.probe_status(logger)


    def on_group_view_change(self, members, view_id_changed):
        """
        Query membership info about the cluster and update labels and
        annotations in each pod.

        This is for monitoring only and should not trigger any changes other
        than in informational k8s fields.
        """
        for pod in self.cluster.get_pods():
            info = pod.get_membership_info()
            if info:
                pod_member_id = info.get("memberId")
            else:
                pod_member_id = None

            for member_id, role, status, view_id, endpoint, version in members:
                if pod_member_id and member_id == pod_member_id:
                    pass
                elif endpoint == pod.endpoint:
                    pass
                else:
                    continue
                pod.update_membership_status(member_id, role, status, view_id, version)
                if status == "ONLINE":
                    pod.update_member_readiness_gate("ready", True)
                else:
                    pod.update_member_readiness_gate("ready", False)
                break


    def on_upgrade(self, version):
        # TODO check if version change is valid
        pass