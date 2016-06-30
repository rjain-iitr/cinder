import string
import random
from Crypto.Cipher import AES
from oslo_log import log as logging
import base64
import MySQLdb
from cinder import exception

LOG = logging.getLogger(__name__)

MASTER_KEY="558MI2LKK86CGAJMBVNNCD3KIPZTA4SY"
def CreateEncryptedKey(project_id):
       user_key=_get_user_deprecated_key(project_id)
       encrypted_key=_create_new_volume_encrypted_key(user_key)
       return encrypted_key

def GetPlainTextKey(project_id,encrypted_key):
       user_key=_check_user_encrypted_key(project_id)
       if user_key is None: 
           raise exception.NotAuthorized()
       #TODO Throw exception
       user_decrypted_key=_decrypte_key(user_key,MASTER_KEY)
       decrypted_key=_decrypte_key(encrypted_key,user_decrypted_key)
       return decrypted_key

def _get_user_deprecated_key(project_id):
       user_key=_check_user_encrypted_key(project_id)
       if user_key is None:
          user_key=_create_user_encrypted_key(project_id)
       return _decrypte_key(user_key,MASTER_KEY)
       
def _check_user_encrypted_key(project_id):
       user_key=_check_in_db(project_id)
       return user_key

def _create_user_encrypted_key(project_id):
       val=0
       count =0
       retries=100
       encrypted_key=None
       while val==0 and count<retries:
           user_key=_create_new_user_key()
           encrypted_key=_encrypt_key(user_key,MASTER_KEY)
           val= _save_in_db(project_id,encrypted_key)
           count=count+1
       if val==0 or val==2:
             raise Exception('Unable to write in db') 
       return encrypted_key

def _check_in_db(project_id):
       db = MySQLdb.connect("localhost","key_user","kmis_pass","kims" )
       cursor = db.cursor()
       sql = "SELECT user_key FROM userkeys WHERE project_id ='%s'" % (project_id)
       result=None
       try:
          cursor.execute(sql)
          row = cursor.fetchone() 
          if row is not None:
             result= row[0]
       except Exception as e:
             LOG.error("Unable to fetch from db",e)

       cursor.close()
       db.close()

       return result

def _save_in_db(project_id,user_key):
       db = MySQLdb.connect("localhost","key_user","kmis_pass","kims" )
       cursor = db.cursor()
       returnval=1
       sql = "INSERT INTO userkeys(project_id, user_key) \
       VALUES ('%s', '%s' )" % \
       (project_id, user_key)
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

def _create_new_volume_encrypted_key(user_key):
       volume_key=_create_new_volume_key()
       encrypted_key=_encrypt_key(volume_key,user_key)
       return encrypted_key

def _create_new_volume_key():
       #64 Cipher_key + 16 IV
       key=id_generator(80)
       return key

def _create_new_user_key():
       key=id_generator(32)
       return key

def _encrypt_key(key,encryption_key):
       encryption_suite = AES.new(encryption_key, AES.MODE_ECB)
       cipher_text = base64.b64encode(encryption_suite.encrypt(key))
       return cipher_text

def _decrypte_key(key,encryption_key):
       decryption_suite = AES.new(encryption_key, AES.MODE_ECB)
       plain_text = decryption_suite.decrypt(base64.b64decode(key))
       return plain_text


 
def id_generator(size, chars=string.ascii_uppercase + string.digits):
   return ''.join(random.choice(chars) for _ in range(size))
