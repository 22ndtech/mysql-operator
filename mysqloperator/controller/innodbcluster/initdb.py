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
 
from ..shellutils import Session
from .. import mysqlutils
import mysqlsh
import time
import os

def start_clone_seed_pod(session, cluster, seed_pod, clone_spec, logger):
    logger.info(f"Initializing seed instance. method=clone  pod={seed_pod}  source={clone_spec.uri}")

    donor_root_co = dict(mysqlsh.globals.shell.parse_uri(clone_spec.uri))
    donor_root_co["user"] = clone_spec.root_user or "root"
    donor_root_co["password"] = clone_spec.get_root_password(cluster.namespace)

    with Session(donor_root_co) as donor:
        clone_installed = False
        for row in iter(donor.run_sql("SHOW PLUGINS").fetch_one, None):
            if row[3]:
                logger.info(f"Donor has plugin {row[0]} / {row[3]}")
                if row[0] == "clone":
                    clone_installed = True
        
        if not clone_installed:
            logger.info(f"Installing clone plugin at {donor.uri}")
            donor.run_sql("install plugin clone soname 'mysql_clone.so'")

        # TODO copy other installed plugins(?)

    # clone
    try:
        donor_co = dict(mysqlsh.globals.shell.parse_uri(clone_spec.uri))
        donor_co["password"] = clone_spec.get_password(cluster.namespace)

        with Session(donor_co) as donor:
            return mysqlutils.clone_server(donor_co, donor, session, logger)
    except mysqlsh.Error as e:
        if mysqlutils.is_client_error(e.code) or e.code == mysqlsh.globals.mysql.ErrorCode.ER_ACCESS_DENIED_ERROR:
            # TODO check why are we still getting access denied here, the container should have all accounts ready by now
            # rethrow client and retriable errors
            raise
        else:
            raise


def monitor_clone(session, start_time, logger):
    logger.info("Waiting for clone...")
    while True:
        r = session.run_sql("select * from performance_schema.clone_progress")
        time.sleep(1)


def finish_clone_seed_pod(session, cluster, logger):
    return
    logger.info(f"Finalizing clone")

    # copy sysvars that affect data, if any
    # TODO

    logger.info(f"Clone finished successfully")



def load_dump(session, cluster, pod, init_spec, logger):
    options = init_spec.loadOptions.copy()

    if init_spec.storage.ociObjectStorage:
        if init_spec.name:
            path = os.path.join(init_spec.prefix or "", init_spec.name)
        else:
            path = init_spec.prefix
        options["osBucketName"] = init_spec.storage.ociObjectStorage.osBucketName
        options["ociConfigFile"] = "/.oci/config"
        options["ociProfile"] = "DEFAULT"
    else:
        path = os.path.join(init_spec.prefix or "", init_spec.path)

    logger.info(f"Executing load_dump({path}, {options})")
    try:
        mysqlsh.globals.util.load_dump(path, options)
    except mysqlsh.Error as e:
        logger.error(f"Error loading dump: {e}")
        raise

