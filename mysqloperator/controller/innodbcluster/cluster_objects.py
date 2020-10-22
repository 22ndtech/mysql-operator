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

from .. import utils, config, consts
import yaml
from ..kubeutils import api_core, api_apps
import base64


# This service includes all instances, even those that are not ready
def prepare_cluster_service(spec):
    tmpl = f"""
apiVersion: v1
kind: Service
metadata:
  name: {spec.name}-instances
  namespace: {spec.namespace}
  labels:
    cluster: {spec.name}
  annotations:
    service.alpha.kubernetes.io/tolerate-unready-endpoints: "true"
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  ports:
  - name: mysql
    port: {spec.mysql_port}
    targetPort: {spec.mysql_port}
  - name: mysqlx
    port: {spec.mysql_xport}
    targetPort: {spec.mysql_xport}
  - name: gr-xcom
    port: {spec.mysql_grport}
    targetPort: {spec.mysql_grport}
  selector:
    app: mysql
    mysql.oracle.com/cluster: {spec.name}
  type: ClusterIP
"""
    return yaml.safe_load(tmpl)



def prepare_secrets(spec):
    def encode(s):
        return base64.b64encode(bytes(s, "ascii")).decode("ascii")

    admin_user = encode(config.CLUSTER_ADMIN_USER_NAME)
    admin_pwd = encode(utils.generate_password())

    tmpl = f"""
apiVersion: v1
kind: Secret
metadata:
  name: {spec.name}-privsecrets
data:
  clusterAdminUsername: {admin_user}
  clusterAdminPassword: {admin_pwd}
"""
    return yaml.safe_load(tmpl)



# TODO - check if we need to add a finalizer to the sts and svc (and if so, what's the condition to remove them)
# TODO - check if we need to make readinessProbe take into account innodb recovery times

# ## About lifecycle probes:
#
# ### startupProbe
#
# used to let k8s know that the container is still starting up.
#
# * Server startup can take anywhere from a few seconds to several minutes.
# * If the server is initializing for the first time, it will take a few seconds.
# * If the server is restarting after a clean shut down and there's not much data,
#   it will take even less to startup.
# * But if it's restarting after a crash and there's a lot of data, the InnoDB
#   recovery can take a very long time to finish.
# Since we want success to be reported asap, we set the interval to a small value.
# We also set the successThreshold to > 1, so that we can report success once
# every now and then to reset the failure counter.
# NOTE: Currently, the startup probe will never fail the startup. We assume that
# mysqld will abort if the startup fails. Once a method to check whether the
# server is actually frozen during startup, the probe should be updated to stop
# resetting the failure counter and let it actually fail.
#
# ### readinessProbe
# 
# used to let k8s know that the container can be marked as ready, which means
# it can accept external connections. We need mysqld to be always accessible,
# so the probe should always succeed as soon as startup succeeds.
# Any failures that happen after it's up don't matter for the probe, because
# we want GR and the operator to control the fate of the container, not the
# probe.
#
# ### livenessProbe
#
# this checks that the server is still healthy. If it fails above the threshold
# (e.g. because of a deadlock), the container is restarted.
#
def prepare_cluster_stateful_set(spec):
    mysql_argv = ["mysqld", "--user=mysql"]
    if config.enable_mysqld_general_log:
        mysql_argv.append("--general-log=1")

    tmpl = f"""
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {spec.name}
  labels:
    mysql.oracle.com/cluster: {spec.name}
spec:
  serviceName: {spec.name}-instances
  replicas: {spec.instances}
  selector:
    matchLabels:
      app: mysql
      mysql.oracle.com/cluster: {spec.name}
  template:
    metadata:
      labels:
        app: mysql
        mysql.oracle.com/cluster: {spec.name}
    spec:
      subdomain: {spec.name}
      readinessGates:
      - conditionType: "mysql.oracle.com/ready"
      initContainers:
      - name: init
        image: {spec.shellImage}
        imagePullPolicy: {spec.shell_image_pull_policy}
        command: ["mysqlsh", "--pym", "mysqloperator", "init"]
        env:
        - name: MY_POD_NAME
          valueFrom:
            fieldRef:
              fieldPath: metadata.name
        - name: MY_POD_NAMESPACE
          valueFrom:
            fieldRef:
              fieldPath: metadata.namespace
        volumeMounts:
        - name: initconfdir
          mountPath: /mnt/initconf
          readOnly: true
        - name: datadir
          mountPath: /var/lib/mysql
        - name: mycnfdata
          mountPath: /mnt/mycnfdata
      containers:
      - name: sidecar
        image: {spec.shellImage}
        imagePullPolicy: {spec.shell_image_pull_policy}
        command: ["mysqlsh", "--pym", "mysqloperator", "sidecar"]
        env:
        - name: MY_POD_NAME
          valueFrom:
            fieldRef:
              fieldPath: metadata.name
        - name: MY_POD_NAMESPACE
          valueFrom:
            fieldRef:
              fieldPath: metadata.namespace
        volumeMounts:
        - name: rundir
          mountPath: /var/run/mysql
        - name: mycnfdata
          mountPath: /etc/my.cnf.d
          subPath: my.cnf.d
        - name: mycnfdata
          mountPath: /etc/my.cnf
          subPath: my.cnf
      - name: mysql
        image: {spec.image}
        imagePullPolicy: {spec.mysql_image_pull_policy}
        args: {mysql_argv}
        lifecycle:
          preStop:
            exec:
              command: ["mysqladmin", "-ulocalroot", "shutdown"]
        terminationGracePeriodSeconds: 60 # TODO check how long this has to be
        startupProbe:
          exec:
            command: ["/livenessprobe.sh", "8"]
          initialDelaySeconds: 5
          periodSeconds: 3
          failureThreshold: 10000
          successThreshold: 1
          timeout: 2
        readinessProbe:
          exec:
            command: ["/readinessprobe.sh"]
          periodSeconds: 5
          initialDelaySeconds: 10
          failureThreshold: 10000
        livenessProbe:
          exec:
            command: ["/livenessprobe.sh"]
          initialDelaySeconds: 15
          periodSeconds: 15
          failureThreshold: 10
          successThreshold: 1
          timeout: 5
        env:
        - name: MYSQLD_PARENT_PID
          value: "0"
          name: MYSQL_ROOT_PASSWORD
          value: "initpass"
{utils.indent(spec.extra_env, 8)}
        ports:
        - containerPort: {spec.mysql_port}
          name: mysql
        - containerPort: {spec.mysql_xport}
          name: mysqlx
        - containerPort: {spec.mysql_grport}
          name: gr-xcom
        volumeMounts:
        - name: datadir
          mountPath: /var/lib/mysql
        - name: rundir
          mountPath: /var/run/mysql
        - name: mycnfdata
          mountPath: /etc/my.cnf.d
          subPath: my.cnf.d
        - name: mycnfdata
          mountPath: /etc/my.cnf
          subPath: my.cnf
        - name: initconfdir
          mountPath: /livenessprobe.sh
          subPath: livenessprobe.sh
        - name: initconfdir
          mountPath: /readinessprobe.sh
          subPath: readinessprobe.sh
      volumes:
      - name: mycnfdata
        emptyDir: {{}}
      - name: rundir
        emptyDir: {{}}
      - name: initconfdir
        configMap:
          name: {spec.name}-initconf
          defaultMode: 0555
  volumeClaimTemplates:
  - metadata:
      name: datadir
    spec:
      accessModes: [ "ReadWriteOnce" ]
      resources:
        requests:
          storage: 2Gi
"""

    statefulset = yaml.safe_load(tmpl.replace("\n\n", "\n"))

    if spec.podSpec:
        utils.merge_patch_object(statefulset["spec"]["template"]["spec"],
                            spec.podSpec, "spec.podSpec")

    if spec.volumeClaimTemplates:
        utils.merge_patch_object(statefulset["spec"]["volumeClaimTemplates"],
                            spec.volumeClaimTemplates, "spec.volumeClaimTemplates",
                            key=".metadata.name")

    return statefulset


def prepare_initconf(spec):
    liveness_probe = """#!/bin/bash
# Copyright (c) 2020, Oracle and/or its affiliates.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA

# Insert 1 success every this amount of failures
# (assumes successThreshold is > 1)
max_failures_during_progress=$1

# Ping the server to see if it's up
mysqladmin -umysqlhealthchecker ping
# If it's up, we succeed
if [ $? -eq 0 ]; then
  exit 0
fi

if [ -z $max_failures_during_progress ]; then
  exit 1
fi

# If the init/startup/InnoDB recovery is still ongoing, we're
# not succeeded nor failed yet, so keep failing and getting time
# extensions until it succeeds.
# We currently rely on the server to exit/abort if the init/startup fails,
# but ideally there would be a way to check whether the server is
# still making progress and not just stuck waiting on a frozen networked
# volume, for example.

if [ -f /fail-counter ]; then
  fail_count=$(($(cat /fail-counter) + 1))
else
  fail_count=1
fi

if [ $fail_count -gt $max_failures_during_progress ]; then
  # Report success to reset the failure counter upstream and get
  # a time extension
  rm -f /fail-counter
  exit 0
else
  # Update the failure counter and fail out
  echo $fail_count > /fail-counter
  exit 1
fi
"""

    readiness_probe = """#!/bin/bash
# Copyright (c) 2020, Oracle and/or its affiliates.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA

# Once the container is ready, it's always ready.
if [ -f /mysql-ready ]; then
  exit 0
fi

# Ping server to see if it is ready
if mysqladmin -umysqlhealthchecker ping; then
  touch /mysql-ready
  exit 0
else
  exit 1
fi
"""

    tmpl = f"""
apiVersion: v1
kind: ConfigMap
metadata:
  name: {spec.name}-initconf
data:
  readinessprobe.sh: |
{utils.indent(readiness_probe, 4)}


  livenessprobe.sh: |
{utils.indent(liveness_probe, 4)}


  my.cnf.in: |
    # Server identity related options (not shared across instances).
    # Do not edit.
    [mysqld]
    server_id=@@SERVER_ID@@
    report_host=@@HOSTNAME@@
    datadir=/var/lib/mysql
    loose_mysqlx_socket=/var/run/mysql/mysqlx.sock
    socket=/var/run/mysql/mysql.sock

    [mysql]
    socket=/var/run/mysql/mysql.sock

    [mysqladmin]
    socket=/var/run/mysql/mysql.sock

    !includedir /etc/my.cnf.d


  00-basic.cnf: |
    # Basic configuration.
    # Do not edit.
    [mysqld]
    plugin_load_add=auth_socket.so
    loose_auth_socket=FORCE_PLUS_PERMANENT
    skip_log_error
    log_error_verbosity=3

  01-group_replication.cnf: |
    # GR and replication related options
    # Do not edit.
    [mysqld]
    log_bin
    enforce_gtid_consistency=ON
    gtid_mode=ON
    relay_log_info_repository=TABLE


  99-extra.cnf: |
    # Additional user configurations taken from spec.mycnf in InnoDBCluster.
    # Do not edit directly.
{utils.indent(spec.mycnf, 4) if spec.mycnf else ""}


"""
    return yaml.safe_load(tmpl)


def update_stateful_set_spec(sts, patch):
    api_apps.patch_namespaced_stateful_set(
        sts.metadata.name, sts.metadata.namespace, body=patch)


def update_version(sts, spec):
    patch = {"spec": {"template": {"spec": {"containers": [
      {"name": "mysql", "image": spec.image}
    ]}}}}

    update_stateful_set_spec(sts, patch)


def on_first_cluster_pod_created(cluster, logger):
    # Add finalizer to the cluster object to prevent it from being deleted
    # until the last pod is properly deleted.
    cluster.add_cluster_finalizer()


def on_last_cluster_pod_removed(cluster, logger):
    # Remove cluster finalizer because the last pod was deleted, this lets
    # the cluster object to be deleted too
    logger.info(f"Last pod for cluster {cluster.name} was deleted, removing cluster finalizer...")
    cluster.remove_cluster_finalizer()