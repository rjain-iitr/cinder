# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
# Copyright (c) 2016 Reliance JIO Corporation
# Copyright (c) 2016 Shishir Gowda <shishir.gowda@ril.com>
# Most of this work is directly derived from ceph, swift and chunked drivers
"""Ceph Backup Service Implementation.

This driver supports backuping up ceph volumes to a s3 like object store.

It is capable of performing incremental backups.

If incremental backups are used, multiple backups of the same volume are stored
as snapshots so that minimal space is consumed in the object store and
restoring the volume takes a far reduced amount of time compared to a full
copy.

Note that Cinder supports restoring to a new volume or the original volume the
backup was taken from. For the latter case, a full copy is enforced since this
was deemed the safest action to take. It is therefore recommended to always
restore to a new volume (default).
"""

import fcntl
import os
import re
import subprocess
import time
import boto
import boto.s3.connection
import pdb
import eventlet
import math
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import encodeutils
from oslo_utils import excutils
from oslo_utils import units
from oslo_utils import timeutils
from filechunkio import FileChunkIO

from cinder.backup import driver
from cinder import exception
from cinder.i18n import _, _LE, _LI, _LW
from cinder import utils
import cinder.volume.drivers.rbd as rbd_driver

try:
    import rbd
    import rados
except ImportError:
    rbd = None
    rados = None

LOG = logging.getLogger(__name__)

service_opts = [
    cfg.StrOpt('sbs_access_key', default='',
               help='Access key for S3 store.'),
    cfg.StrOpt('sbs_secret_key', default='',
               help='Secrete key for S3 store.'),
    cfg.StrOpt('sbs_container', default='sbs-backup',
               help='Bucket in S3 store to save backups.'),
    cfg.StrOpt('backup_ceph_user', default='cinder',
		help='user'),
    cfg.StrOpt('backup_ceph_pool', default='sbs',
		help='pool sbs'),
    cfg.StrOpt('backup_ceph_conf', default='/etc/ceph/ceph.conf',
		help='ceph conf'),
    cfg.StrOpt('sbs_dss_host', default='',
        help='endpoint of object store gateway'),
]

CONF = cfg.CONF
CONF.register_opts(service_opts)

class SBSBackupDriver(driver.BackupDriver):
    """Backup Cinder volumes to S3 like Object Store.

    The backup will be performed using incremental differential backups which
	 *should* give a performance gain.
    """
    def __init__(self, context, db_driver=None, execute=None):
        super(SBSBackupDriver, self).__init__(context, db_driver)
        self.rbd = rbd
        self.rados = rados
        self._execute = execute or utils.execute
        self._access_key = encodeutils.safe_encode(CONF.sbs_access_key)
        self._secret_key = encodeutils.safe_encode(CONF.sbs_secret_key)
        self._container = encodeutils.safe_encode(CONF.sbs_container)
        self._ceph_backup_user = encodeutils.safe_encode(CONF.backup_ceph_user)
        self._ceph_backup_pool = encodeutils.safe_encode(CONF.backup_ceph_pool)
        self._ceph_backup_conf = encodeutils.safe_encode(CONF.backup_ceph_conf)
        self._dss_host = encodeutils.safe_encode(CONF.sbs_dss_host)

    #Routine used to connect to ceph cluster called by rbd_driver.RADOSClient
    def _connect_to_rados(self, pool=None):
        """Establish connection to the backup Ceph cluster."""
        client = self.rados.Rados(rados_id=self._ceph_backup_user,
                                  conffile=self._ceph_backup_conf)
        try:
            client.connect()
            pool_to_open = encodeutils.safe_encode(pool or
                                                   self._ceph_backup_pool)
            ioctx = client.open_ioctx(pool_to_open)
            return client, ioctx
        except self.rados.Error:
            # shutdown cannot raise an exception
            client.shutdown()
            errmsg = _("Failed to connect to rados cluster")
            raise exception.InvalidBackup(reason=errmsg)

    #Routine use to disconnect from ceph cluster
    def _disconnect_from_rados(self, client, ioctx):
        """Terminate connection with the backup Ceph cluster."""
        # closing an ioctx cannot raise an exception
        ioctx.close()
        client.shutdown()

    #Returns base image name as: volume-<volume_id>.backup.base
    def _get_backup_base_name(self, volume_id, backup_id=None,
                              diff_format=False):
        # Ensure no unicode
        rbd_image_name = encodeutils.safe_encode("volume-%s.backup.base" % volume_id)
        LOG.debug("rbd base image name: %s", rbd_image_name)
        return rbd_image_name

    #Returns rbd images name as: backup.<backup_id>.snap.time_stamp
    def _get_rbd_image_name(self, backup):
	#if base image, then volume id == backup id
	if backup['id'] == backup['volume_id']:
	    rbd_image_name = encodeutils.safe_encode("volume-%s.backup.base"
							 % backup['id'])
	else:
            rbd_image_name =  encodeutils.safe_encode("backup.%s.snap.%s" %
                                                 (backup['id'], backup['time_stamp']))
        LOG.debug("rbd image name: %s", rbd_image_name)
        return rbd_image_name

    #RBD snapshot naming pattern: backup.<backup_id>.snap.time_stamp
    @staticmethod
    def backup_snapshot_name_pattern():
        """Returns the pattern used to match backup snapshots.

        It is essential that snapshots created for purposes other than backups
        do not have this name format.
        """
        return r"^backup\.([a-z0-9\-]+?)\.snap\.(.+)$"

    @staticmethod
    def backup_base_name_pattern():
        """Returns the pattern used to match base of backup.

        It is essential that snapshots created for purposes other than backups
        do not have this name format.
        """
        return r"^volume-([a-z0-9\-]+?)\.backup\.base$"

    #Returns snap name as: backup.<backup_id>.snap.<%0.2f time_stamp>
    @staticmethod
    def _get_new_snap_name(backup_id):
        time_stamp = (_("%0.2f" % time.time()))
        return (time_stamp, encodeutils.safe_encode("backup.%s.snap.%s" %
                                                   (backup_id, time_stamp)))

    @staticmethod
    def _get_volume_size_gb(volume):
        """Return the size in gigabytes of the given volume.

        Raises exception.InvalidParameterValue if volume size is 0.
        """
        if int(volume['size']) == 0:
            errmsg = _("Need non-zero volume size")
            raise exception.InvalidParameterValue(errmsg)

        return int(volume['size']) * units.Gi

    def _validate_string_args(self, *args):
        """Ensure all args are non-None and non-empty."""
        return all(args)

    #Returns args with --id, --conf, --pool for connecting to ceph
    def _ceph_args(self, user, conf=None, pool=None):
        """Create default ceph args for executing rbd commands.

        If no --conf is provided, rbd will look in the default locations e.g.
        /etc/ceph/ceph.conf
        """

        # Make sure user arg is valid since rbd command may not fail if
        # invalid/no user provided, resulting in unexpected behaviour.
        if not self._validate_string_args(user):
            raise exception.BackupInvalidCephArgs(_("invalid user '%s'") %
                                                  user)

        args = ['--id', user]
        if conf:
            args.extend(['--conf', conf])
        if pool:
            args.extend(['--pool', pool])

        return args

    #Also called by volume delete API, to remove all snaps
    @classmethod
    def get_backup_snaps(cls, rbd_image, sort=False):
        """Get all backup snapshots for the given rbd image.

        NOTE: this call is made public since these snapshots must be deleted
              before the base volume can be deleted.
        """
        snaps = rbd_image.list_snaps()
        #first search for base snaps: multiple if restored volume
        backup_snaps = []
        for snap in snaps:
            base_name_pattern = cls.backup_base_name_pattern()
            result = re.search(base_name_pattern, snap['name'])
            if result:
                backup_snaps.append({'name':result.group(0),
                         'backup_id':result.group(1),
                         'timestamp': '0'})

        #find the remaining backups
        for snap in snaps:
            search_key = cls.backup_snapshot_name_pattern()
            result = re.search(search_key, snap['name'])
            if result:
                backup_snaps.append({'name': result.group(0),
                                     'backup_id': result.group(1),
                                     'timestamp': result.group(2)})

        if sort:
            # Sort into ascending order of timestamp
            backup_snaps.sort(key=lambda x: x['timestamp'], reverse=True)

        return backup_snaps
    
    #return most_recent backup taken for same volume
    #If restored volume, latest might defer
    def _get_most_recent_snap(self, rbd_image, volume_id):
        """Get the most recent backup snapshot of the provided image.

        Returns name of most recent backup snapshot or None if there are no
        backup snapshots.
        """
        backup_snaps = self.get_backup_snaps(rbd_image, sort=True)
        if not backup_snaps:
            return None
        num_snaps = len(backup_snaps)

        #get backup info from db. If does not exist, do not use it as from_snap
        # if exists, but volume is different, then it is part of restored op
        # Do not use the above too. 
	tmp_list = []
        for i in range(num_snaps):
            backup = self.db.backup_get(self.context,backup_snaps[i]['backup_id'])
            if (backup != None) and (backup['volume_id'] == volume_id):
		tmp_list.append(backup_snaps[i])

        #if all of the snaps are either deleted or not part of same volume, return none
        if len(tmp_list) == 0:
            return None
        else:
            return tmp_list[0]['name']

    #First snap created is the base
    def _lookup_base(self, rbd_image, volume_id):
        backup_snaps = self.get_backup_snaps(rbd_image, sort=False)
        if not backup_snaps:
            return None
        backup_snaps.sort(key=lambda x: x['timestamp'], reverse=False)
        found_base = False
        length = len(backup_snaps)
        #Find the actual base. If we are taking backup of restored volumes
        #then we will have multiple volume-id.base.snap. Match id to current
        # volume id
        for i in range(length):
            if backup_snaps[i]['backup_id'] == volume_id:
                found_base = True
                break

        if found_base:
            return backup_snaps[i]['name']
        else:
            return None

    #returns a handle to snap with key_name = snap_name
    def _get_snap_handle_from_DSS(self, bucket, key_name):
        if (bucket == None) or (key_name == None):
            return
        try:
            key = bucket.get_key(key_name)
        except Exception as e:
            errmsg = (_("Failed to get handle for snap %s") % key_name)
            LOG.error(errmsg)
            raise exception.InvalidBackup(reason=errmsg)
        return key

    #Check if base and/or snapshot exists in DSS
    def _snap_exists(self, base_name, snap_name):
        if base_name == None:
            return False

        conn = self._connect_to_DSS()
        if conn != None:
            bucket = self._get_bucket(conn, self._container)
        else:
            return False

        if bucket != None:
            key_base = self._get_snap_handle_from_DSS(bucket, base_name)
            if key_base == None:
                return False
        else:
            return False

        if base_name == snap_name:
            return True

        if snap_name:
            key_snap = self._get_snap_handle_from_DSS(bucket, snap_name)
            if key_snap == None:
                return False
        return True

    #connect to object store and return handle
    def _connect_to_DSS(self):
        try:
            LOG.info("Connecting to dss @ %s" % self._dss_host)
            conn = boto.connect_s3(host=self._dss_host,aws_access_key_id=self._access_key,
                                   aws_secret_access_key=self._secret_key, is_secure=True,
                                   calling_format = boto.s3.connection.OrdinaryCallingFormat(),)
        except Exception as e:
            errmsg = (_("Exception getting connection to object store %s") % e)
            LOG.error(errmsg)
            raise exception.BackupRBDOperationFailed(errmsg)
            return None
        return conn

    #return handle to the bucket
    def _get_bucket(self, conn, bucket_name):
        backup_bucket = None
        if (conn != None) and (bucket_name != None):
            try:
                backup_bucket = conn.get_bucket(bucket_name)
            except Exception as e:
                LOG.warn("Exception getting bucket %s" % bucket_name)
                return None
        return backup_bucket

    #currently broken, not used
    def _multi_part_upload(self, bucket, key, loc):
        size = os.stat(loc).st_size
        mp = bucket.initiate_multipart_upload(key)
        chunk_size = 52428800
        chunk_count = int(math.ceil(size/float(chunk_size)))

        for i in range(chunk_count):
            off_set = chunk_size * i
            bytes = min(chunk_size, size - off_set)
            with FileChunkIO(loc, 'r', offset=off_set, bytes=bytes) as fp:
                mp.upload_part_from_file(fp,part_num=i+1)

        mp.complete_upload()

    def _upload_to_DSS(self, snap_name, volume_name, ceph_args, from_snap=None):
        tmp_cmd = ['mkdir', '-p', '/tmp/uploads']
        self._execute(*tmp_cmd, run_as_root=False)
        cmd = ['rbd', 'export-diff'] + ceph_args
        #if from_snap is None, do full upload
        if from_snap is not None:
            cmd.extend(['--from-snap', from_snap])
        path = encodeutils.safe_encode("%s@%s" %
                                      (volume_name, snap_name))
        loc = encodeutils.safe_encode("/tmp/uploads/%s" % (snap_name))
        cmd.extend([path, loc])
        LOG.info(cmd)
        self._execute (*cmd, run_as_root=False)
        conn = self._connect_to_DSS()
        bucket = None
        if conn != None:
            bucket = self._get_bucket(conn, self._container)
        if bucket == None:
            os.remove(loc)
            return

        key = bucket.new_key(snap_name)
        if key == None:
            os.remove(loc)
            msg = (_("Failed to create backup entry %s in object store") % (snap_name))
            LOG.error(msg)
            raise exception.BackupOperationError(msg)
            return
        try:
            self._multi_part_upload(bucket, key, loc)
            #key.set_contents_from_filename(loc)
        except exception as e:
            os.remove(loc)
            msg = (_("Failed to upload backup % to object store") % (snap_name))
            LOG.error(msg)
            raise exception.BackupOperationError(msg)

        os.remove(loc)
        return

    """
    1. If 1st snapshot or missing base or missing incr snap
        create new snapshot (without incr) and treat it as base
        take snapshot from base (incr) with given name (size might be 0)
        upload/store both base and incr snap
    2. If incr snapshot
        create incr snapshot w.r.t latest snap
        upload/store snapshot
    """

    def _check_create_base(self, volume_id, volume_file, volume_name, 
			   base_name, ceph_args, backup_host, backup_service, from_snap=None):

        #Create an incremental backup from an RBD image.
        rbd_user = volume_file.rbd_user
        rbd_pool = volume_file.rbd_pool
        rbd_conf = volume_file.rbd_conf
        source_rbd_image = volume_file.rbd_image
        # Check if base image exists in dest
        found_base_image = self._lookup_base(source_rbd_image, volume_id)
        #If base image not found, create base image, might be 1st snap
        if not found_base_image:
            # since base image is missing, default to full snap.Cleanup too
            if from_snap:
                LOG.debug("Source snapshot '%(snapshot)s' of volume "
                          "%(volume)s is stale.",
                          {'snapshot': from_snap, 'volume': volume_id})
		        #do not delete snaps: backups of restored volumes
                #source_rbd_image.remove_snap(from_snap)
                #source_rbd_image.remove_snap(base_name)
                from_snap = None

            #TODO: update size
            #Create new base image and upload it, so from-snap also becomes base
            LOG.debug ("Creating base image %s for volume %s" % (base_name, volume_id))
            source_rbd_image.create_snap(base_name)
            desc = (_("Base image of volume '%(volume)s'") % {'volume':volume_id})
            options = {'user_id': self.context.user_id,
                       'project_id': self.context.project_id,
                       'display_name': base_name,
                       'display_description': desc,
                       'volume_id': volume_id,
                       'id': volume_id,
                       'status': 'available',
                       'container': self._container,
                       'host': backup_host,
                       'service': 'cinder.backup.drivers.sbs',
                       'size': "2",
                      }
            backup = self.db.backup_create(self.context, options)
            self._upload_to_DSS(base_name, volume_name, ceph_args)
            from_snap = base_name
        else:
            # If a from_snap is defined but does not exist in the back base
            # then we cannot proceed (see above)
            if not self._snap_exists(base_name, from_snap):
                errmsg = (_("Snapshot='%(snap)s' does not exist in base "
                            "image='%(base)s' - aborting incremental "
                            "backup") %
                          {'snap': from_snap, 'base': base_name})
                LOG.error(errmsg)
                # Raise this exception so that caller can try another
                # approach
                raise exception.BackupRBDOperationFailed(errmsg)


        return (base_name, from_snap)

    def _backup_rbd(self, backup, volume_file, volume):
        #Create an incremental backup from an RBD image.
        rbd_user = volume_file.rbd_user
        rbd_pool = volume_file.rbd_pool
        rbd_conf = volume_file.rbd_conf
        source_rbd_image = volume_file.rbd_image
        backup_id = backup['id']
        backup_host = backup['host']
        backup_service = backup['service']
        volume_id = volume['id']
        volume_name = volume['name']

        # Identify our --from-snap point (if one exists)
        from_snap = self._get_most_recent_snap(source_rbd_image, volume_id)
        base_name = self._get_backup_base_name(volume_id, diff_format=True)
        ceph_args = self._ceph_args(rbd_user, rbd_conf, pool=rbd_pool)

        #check base snap and from_snap and create base if missing
        base_name, from_snap = self._check_create_base(volume_id, volume_file,
                                                       volume_name, base_name,
                                                       ceph_args, backup_host,
                                                       backup_service, from_snap)

        # Snapshot source volume so that we have a new point-in-time
        time_stamp, new_snap = self._get_new_snap_name(backup_id)
        LOG.debug("Creating backup %s", new_snap)
        source_rbd_image.create_snap(new_snap)
        LOG.debug("Using --from-snap '%(snap)s' for incremental backup of "
                  "volume %(volume)s, with base image '%s(base)s'.",
                    {'snap': from_snap, 'volume': volume_id,
                     'base': base_name})

        # export diff now
        self._upload_to_DSS(new_snap, volume_name, ceph_args, from_snap)

        #if from_snap is same as base, then parent is base
        if from_snap == base_name:
            par_id = volume_id
        else:
            #extract out the id from the snap name
            search_key = SBSBackupDriver.backup_snapshot_name_pattern()
            result = re.search(search_key, from_snap)
            if result:
                par_id = result.group(1)
            else:
                msg = (_("backup id of parent not found for snap '%(snap)s'")
                          % backup['id'])
                LOG.err(msg)
                raise exception.BackupOperationError(mesg)

        #make sure snap is newer than base
        now = timeutils.utcnow()
        self.db.backup_update(self.context, backup_id,
			      {'created_at': now})
        self.db.backup_update(self.context, backup_id,
			      {'time_stamp': time_stamp})

        self.db.backup_update(self.context, backup_id,
                              {'parent_id': par_id})
        self.db.backup_update(self.context, backup_id,
                              {'container': self._container})

        # Remove older from-snap from src, as new snap will be "New" from-snap
        # Do this is from-snap is not same as base snap, as it will be the first
        #currently we do not want to remove snaps to support out of order deletion
        #if from_snap != base_name:
        	#source_rbd_image.remove_snap(from_snap)
        #return

    #TODO: Generate/update _container/bucket name and use that in DSS
    def backup(self, backup, volume_file, backup_metadata=False):
        backup_id = backup['id']
        volume = self.db.volume_get(self.context,backup['volume_id'])
        volume_id = volume['id']

        LOG.debug("Starting backup of volume='%s'.", volume_id)

        # Ensure we are at the beginning of the volume
        volume_file.seek(0)
        length = self._get_volume_size_gb(volume)

        self._backup_rbd(backup, volume_file, volume)

        self.db.backup_update(self.context, backup_id,
                              {'container': self._container})
        return

    #return sorted list with base as [0], and backup as last[n-1]
    def _list_incr_backups(self, backup):
        parent_id = backup['parent_id']

        backup_tree = []
        backup_tree.append(backup)
        curr = backup
        while curr['parent_id']:
            parent_backup = self.db.backup_get(self.context,
                                               curr['parent_id'])
            LOG.debug("Got parent of backup %s as %s" % (curr['id'], curr['parent_id']))
            backup_tree.append(parent_backup)
            curr = parent_backup

        backup_tree.reverse()
        return backup_tree

    def _download_from_DSS(self, snap_name, volume_name, ceph_args):
        tmp_cmd = ['mkdir', '-p', '/tmp/downloads']
        self._execute(*tmp_cmd, run_as_root=False)
        loc = encodeutils.safe_encode("/tmp/downloads/%s" % (snap_name))
        open(loc,'a').close

        conn = self._connect_to_DSS()
        bucket = None
        if conn != None:
            bucket = self._get_bucket(conn, self._container)

        if bucket == None:
            return

        key = self._get_snap_handle_from_DSS(bucket, snap_name)
        if key == None:
            return

        try:
            key.get_contents_to_filename(loc)
        except Exception as e:
            errmsg = (_("Failed to get contents of backup %s from object store") % (snap_name))
            LOG.error(errmsg)
            raise exception.InvalidBackup(reason=errmsg)
            return
        cmd = ['rbd', 'import-diff'] + ceph_args
        #if from_snap is None, do full upload
        volume_name = encodeutils.safe_encode("%s" % (volume_name))
        cmd.extend([loc, volume_name])
        LOG.info("Downloading backups %s" % (cmd))

        self._execute (*cmd, run_as_root=False)
        os.remove(loc)
        return

    def _restore_rbd(self, backup, volume_id, volume_file, ceph_args):
        backup_id = backup['id']
        backup_volume_id = backup['volume_id']
        # issue here is, we cant resolve timestamp suffix of rbd image
        backup_name = self._get_rbd_image_name(backup)
        volume = self.db.volume_get(self.context,volume_id)
        length = int(volume['size']) * units.Gi
        volume_name = (_("volume-%s" % (volume['id'])))
        LOG.debug("Restoring backup %s to volume %s" % (backup_name, volume_name))
        # If the volume we are restoring to is the volume the backup was
        # made from, force a full restore since a diff will not work in
        # this case.

        #TODO: handle restoring on same source volume
        if volume_id == backup_volume_id:
            LOG.debug("Destination volume is same as backup source volume")
            return False

        self._download_from_DSS(backup_name, volume_name, ceph_args) 
        return

    """
        Get backup and all its parent leading upto base
        Replay in reverse order, from base to specified backup
        resize image to original size, as it might get shrunk
        due to replay of diffs
    """
    def restore(self, backup, volume_id, volume_file):
        backup_id = backup['id']
        rbd_user = volume_file.rbd_user
        rbd_pool = volume_file.rbd_pool
        rbd_conf = volume_file.rbd_conf
        backup_tree = self._list_incr_backups(backup)
        ceph_args = self._ceph_args(rbd_user, rbd_conf, pool=rbd_pool)
        backup_layers = len(backup_tree)
        try:
            i = 0
            while i < backup_layers:
                backup_diff = backup_tree[i]
                self._restore_rbd(backup_diff, volume_id, volume_file,
                                  ceph_args)
                i = i+1

            # Be tolerant of IO implementations that do not support fileno()
            try:
                fileno = volume_file.fileno()
            except IOError:
                LOG.debug("Restore target I/O object does not support "
                          "fileno() - skipping call to fsync().")
            else:
                os.fsync(fileno)

            LOG.debug('restore %(backup_id)s to %(volume_id)s finished.',
                      {'backup_id': backup_id, 'volume_id': volume_id})

        except exception.BackupOperationError as e:
            LOG.error(_LE('Restore to volume %(volume)s finished with error - '
                          '%(error)s.'), {'error': e, 'volume': volume_id})
            raise
        return

    def _remove_from_DSS(self, backup):
        snap_name = self._get_rbd_image_name(backup)
        LOG.info("Deleting backups %s from container %s" % (snap_name, self._container))

        conn = self._connect_to_DSS()
        if conn != None:
            bucket = self._get_bucket(conn, self._container)
        else:
            errmsg = _("Failed to connect to object store")
            LOG.error(errmsg)
            raise exception.InvalidBackup(reason=errmsg)
            return

        if bucket != None:
            try:
                bucket.delete_key(snap_name)
            except Exception as e:
                errmsg = (_("Failed to delete backup %s from object store") % (snap_name))
                LOG.error(errmsg)
                raise exception.InvalidBackup(reason=errmsg)

        return

    def _delete_snap_from_src(self, backup):
        volume_name = encodeutils.safe_encode("volume-%s" % (backup['volume_id']))
        backup_name = self._get_rbd_image_name(backup)
        LOG.info("Deleting backups %s from src pool" % (backup_name))
        with rbd_driver.RADOSClient(self, self._ceph_backup_pool) as client:
	    backup_rbd = None
            try:
                backup_rbd = self.rbd.Image(client.ioctx, volume_name, read_only=False)
                backup_rbd.remove_snap(backup_name)
            except self.rbd.ImageNotFound:
                LOG.info(_LI("volume %s no longer exists in backend") % volume_name)
                raise
            finally:
                if backup_rbd != None:
                    backup_rbd.close()
        return

    def _delete_backups(self, backup_list):
        last_backup = None
        length = len(backup_list)
        i = 0
        while i < length:
	    backup = backup_list[i]
            LOG.debug("Deleting backup %s" % backup['id'])
            self._remove_from_DSS(backup)
            self._delete_snap_from_src(backup)
            self.db.backup_destroy(self.context, backup['id'])
            last_backup = backup
            i = i+1
        LOG.debug("Last backup deleted %s" % last_backup['id'])
        return last_backup

    def _mark_backup_for_deletion(self, backup):
        self.db.backup_update(self.context, backup['id'],
                              {'status': "deleting"})
        return

    def _incr_backups_to_delete(self, curr, backup_id, backup_list):
        can_delete = True
        while curr['parent_id']:
            #if any snap till given snap is not marked for deletion, fail
            if curr['status'] != "deleting":
                can_delete = False
                break
	         #if parent is given backup to be deleted, do not add it
            if curr['parent_id'] == backup_id:
                break

            backup_list.append(curr)

            LOG.debug("Got parent of backup %s as %s" % (curr['id'], curr['parent_id']))

            parent_backup = self.db.backup_get(self.context,
                                               curr['parent_id'])
            curr = parent_backup

        return (can_delete, backup_list)

    """
    Mark snaps as deleted, but keep them if they are parent of another existing
    snap. Delete the snap only if there are no dependencies on it
    """
    def delete(self, backup):
        """Delete the given backup from Ceph object store."""
        LOG.debug('Delete started for backup=%s', backup['id'])
        # Don't allow backup to be deleted if there are incremental
        # backups dependent on it, mark it for deleted.
        # Find all the dependencies. Only when all dependents are
        # marked for deletion, we can do delete all
        volume_id = backup['volume_id']
        latest_backup = None
        backups = self.db.backup_get_all_by_volume(self.context.elevated(),
                                                   volume_id)
        if backups:
            latest_backup = max(backups, key=lambda x: x['created_at'])
            curr = latest_backup

        can_delete = True
        backup_list = []

        # if latest backup is not same, then check for dependencies,
        # identify and delete backups till given backup if possible
        if backup['id'] != latest_backup['id']:
           can_delete, backup_list  = self._incr_backups_to_delete(curr, backup['id'], backup_list) 
        else:
            backup_list.append(backup)

        last_backup = None
        if can_delete == True:
            last_backup = self._delete_backups(backup_list)
        else:
            self._mark_backup_for_deletion(backup)

        #see if we can clean up more incr backups due to deletion of these snaps
        tmp_list = []
        if (last_backup and last_backup['parent_id']):
            parent_backup = self.db.backup_get(self.context,
                                               last_backup['parent_id']) 
            can_delete, tmp_list = self._incr_backups_to_delete(parent_backup, None, tmp_list)
	    #currently list has the latest backup too, remove it later
        if tmp_list:
            tmp = self._delete_backups(tmp_list)

        LOG.debug("Delete of backup '%(backup)s' for volume "
                  "'%(volume)s' finished.",
                  {'backup': backup['id'], 'volume': backup['volume_id']})
        return

def get_backup_driver(context):
    return SBSBackupDriver(context)