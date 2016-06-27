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

from oslo_log import log as logging
from sqlalchemy import Column, MetaData, Table, Integer,String

from cinder.i18n import _LE

LOG = logging.getLogger(__name__)


def upgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    volumes = Table('volumes', meta, autoload=True)
    encrypted = Column('encrypted', Integer())
    encryption_id = Column('encryption_id', String(255))

    try:
        volumes.create_column(encrypted)
        volumes.update().values(encrypted=0).execute()
        volumes.create_column(encryption_id)
        volumes.update().values(encryption_id=None).execute()
    except Exception:
        LOG.error(_LE("Adding encryption columns to volumes table failed."))
        raise

    backups = Table('backups', meta, autoload=True)
    encryption_id = Column('encryption_id', String(255))
    volume_type_id =Column('volume_type_id',String(32))

    try:
        backups.create_column(encrypted)
        backups.update().values(encrypted=0).execute()
        backups.create_column(encryption_id)
        backups.update().values(encryption_id=None).execute()
        backups.create_column(volume_type_id)
        backups.update().values(volume_type_id=None).execute()
    except Exception:
        LOG.error(_LE("Adding encryption columns to backups table failed."))
        raise

def downgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    volumes = Table('volumes', meta, autoload=True)
    encrypted = volumes.columns.encrypted
    encryption_id = volumes.columns.encryption_id

    try:
        volumes.drop_column(encrypted)
        volumes.drop_column(encryption_id)
    except Exception:
        LOG.error(_LE("Dropping encryption column from volumes table failed."))
        raise


    backups = Table('backups', meta, autoload=True)
    encrypted = backups.columns.encrypted
    encryption_id = backups.columns.encryption_id
    volume_type_id = backups.columns.encryption_id

    try:
        backups.drop_column(encrypted)
        backups.drop_column(encryption_id)
        backups.drop_column(volume_type_id)
    except Exception:
        LOG.error(_LE("Dropping encryption columns from backups table failed."))
        raise
