import string
import random
from Crypto.Cipher import AES
from oslo_log import log as logging
import base64
import MySQLdb
from cinder import exception
#import cinder.db.sqlalchemy.kims_db as kims_db

from datetime import datetime
from datetime import timedelta
import uuid
from oslo_config import cfg


CONF=cfg.CONF



LOG = logging.getLogger(__name__)
separator="---"

def CreateEncryptedKey(project_id):
       cmk_id,version,user_key=_get_user_deprecated_key(project_id)
       encrypted_key=_create_new_volume_encrypted_key(user_key,cmk_id,str(version))
       return encrypted_key

def GetPlainTextKey(project_id,encrypted_key):
       actual_encrypted_key,user_key=_get_user_encrypted_key_with_encrypted_key(project_id,encrypted_key)
       if user_key is None: 
           raise exception.NotAuthorized()
       #TODO Throw exception
       MASTER_KEY=_get_master_key()
       user_decrypted_key=_decrypte_key(user_key,MASTER_KEY)
       decrypted_key=_decrypte_key(actual_encrypted_key,user_decrypted_key)
       return decrypted_key

def _get_user_deprecated_key(project_id):
       MASTER_KEY=_get_master_key()
       cmk_id,version,user_key=_check_and_add_user_encrypted_key(project_id,MASTER_KEY)
       yield cmk_id
       yield version
       yield _decrypte_key(user_key,MASTER_KEY)
       
def _check_and_add_user_encrypted_key(project_id,MASTER_KEY):
       cmk_id,version,created_at,user_key=_check_in_db(project_id)
       current_time=datetime.now() 
       #user_key=kims_db.check_in_db(project_id)
       actual_time=current_time
       if created_at is None:
            version=1
            cmk_id=_create_cmk_id()
       else:
            timediff=current_time-created_at
            days_diff=timediff.days
            rotation_time=CONF.kims_rotation_time
            if days_diff>rotation_time:
                version=version+1
                days_to_add=(days_diff/rotation_time)*rotation_time
                actual_time=created_at+timedelta(days=days_to_add)
       if user_key is None:
         cmk_id,version,user_key=_create_user_encrypted_key(project_id,MASTER_KEY,cmk_id,version,actual_time)
       elif actual_time !=current_time:
         cmk_id,version,user_key=_create_user_encrypted_key(project_id,MASTER_KEY,cmk_id,version,actual_time)
       yield cmk_id
       yield version
       yield user_key

def _get_user_encrypted_key_with_encrypted_key(project_id,encrpyted_key):
       user_key=None
       tokens=encrpyted_key.split(separator) 
       actual_encrypted_key=tokens[0]
       cmk_id=tokens[1]
       version=tokens[2]
       user_key=_check_in_db_encrypted_key(project_id,cmk_id,version)
       #user_key=kims_db.check_in_db(project_id)
       yield actual_encrypted_key
       yield user_key


def _create_user_encrypted_key(project_id,MASTER_KEY,cmk_id,version,created_at):
       val=0
       encrypted_key=None
       user_key=_create_new_user_key()
       encrypted_key=_encrypt_key(user_key,MASTER_KEY)
       val=_save_in_db(project_id,cmk_id,version,created_at,encrypted_key)
       #val=kims_db.save_in_db(project_id,encrypted_key)
       if val==0:
             cmk_id,version,created_at,encrypted_key=_check_in_db(project_id)
             #encrypted_key=kims_db.check_in_db(project_id)
       elif val==2:
             raise Exception('Unable to write in db') 
       yield cmk_id
       yield version
       yield encrypted_key

def _check_in_db(project_id):
       db = MySQLdb.connect("localhost","key_user","kmis_pass","kims" )
       cursor = db.cursor()
       sql = "SELECT cmk_id,version,created_at,user_key FROM cmkkeys WHERE project_id ='%s' order by version desc" % (project_id)
       cmk_id=None
       version=None
       created_at=None
       user_key=None
       try:
          cursor.execute(sql)
          row = cursor.fetchone() 
          if row is not None:
             cmk_id= row[0]
             version= row[1]
             created_at= row[2]
             user_key= row[3]
       except Exception as e:
             LOG.error("Unable to fetch from db",e)

       cursor.close()
       db.close()

       yield cmk_id
       yield version
       yield created_at
       yield user_key

def _check_in_db_encrypted_key(project_id,cmk_id,version):
       db = MySQLdb.connect("localhost","key_user","kmis_pass","kims" )
       cursor = db.cursor()
       sql = "SELECT user_key FROM cmkkeys WHERE project_id ='%s' and cmk_id='%s' and version=%s" % (project_id,cmk_id,version)
       user_key=None
       try:
          cursor.execute(sql)
          row = cursor.fetchone() 
          if row is not None:
             user_key= row[0]
       except Exception as e:
             LOG.error("Unable to fetch from db",e)

       cursor.close()
       db.close()
       return user_key
           

def _save_in_db(project_id,cmk_id,version,created_at,user_key):
       db = MySQLdb.connect("10.140.12.201","key_user","kmis_pass","kims" )
       cursor = db.cursor()
       returnval=1
       sql = "INSERT INTO cmkkeys(project_id, cmk_id,version,created_at,user_key) \
       VALUES ('%s', '%s','%d', '%s','%s' )" % \
       (project_id,cmk_id,version,created_at,user_key)
       try:
          cursor.execute(sql)
          db.commit()
       except MySQLdb.IntegrityError as e:
          if e.args[0]==1062:
              returnval=0
          else:
              returnval=2
       except Exception as e:
          db.rollback()
          returnval=2
          LOG.error("Unable to write in the db",e)

          
       db.close()
       return returnval

def _get_master_key():
       cmk_id,version,created_at,_key=_check_in_db("MASTER_KEY")
       #_key=kims_db.check_in_db("MASTER_KEY")
       if _key is None:
          _generated_key=_create_new_master_key()
          val=_save_in_db("MASTER_KEY",_create_cmk_id(),1,datetime.now(),_generated_key)
          #val=kims_db.save_in_db("MASTER_KEY",_generated_key)
          if val==1 or val==0:
             cmk_id,version,created_at,_key=_check_in_db("MASTER_KEY")
             #_key=kims_db.check_in_db("MASTER_KEY")
          elif val==2:
             raise Exception('Unable to create_Master_key') 
       return _key

def _create_new_volume_encrypted_key(user_key,cmk_id,version):
       volume_key=_create_new_volume_key()
       encrypted_key=_encrypt_key(volume_key,user_key)+separator+cmk_id+separator+version
       return encrypted_key

def _create_new_volume_key():
       #64 Cipher_key + 16 IV
       key1=str(uuid.uuid4())
       key1=string.replace(key1,'-','')
       key2=str(uuid.uuid4())
       key2=string.replace(key2,'-','')
       key3=str(uuid.uuid4())
       key3=string.replace(key3,'-','')
       finalkey=key1+key2+key3[0:16]
       return finalkey

def _create_new_user_key():
       key=str(uuid.uuid4())
       key=string.replace(key,'-','')
       return key

def _encrypt_key(key,encryption_key):
       encryption_suite = AES.new(encryption_key, AES.MODE_ECB)
       cipher_text = base64.b64encode(encryption_suite.encrypt(key))
       return cipher_text

def _decrypte_key(key,encryption_key):
       decryption_suite = AES.new(encryption_key, AES.MODE_ECB)
       plain_text = decryption_suite.decrypt(base64.b64decode(key))
       return plain_text

def _create_new_master_key():
       return _create_new_user_key()

def _create_cmk_id():
       return _create_new_user_key()


 
def id_generator(size, chars=string.ascii_uppercase + string.digits):
   return ''.join(random.choice(chars) for _ in range(size))
