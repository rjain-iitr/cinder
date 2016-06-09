





MASTER_KEY="MASTER_KEY"
def CreateEncryptedKey(project_id):
       user_key=_get_user_deprecated_key(project_id)
       encrypted_key=_create_new_volume_encrypted_key(user_key)
       return encrypted_key

def GetPlainTextKey(project_id,encrypted_key):
       user_key=_check_user_encrypted_key(project_id)
       #if user_key is None: 
       #TODO Throw exception
       decrypted_key=_decrypte_key(encrypted_key,user_key)
       return decrypted_key

def _get_user_deprecated_key(project_id):
       user_key=_check_user_encrypted_key(project_id)
       if user_key is None:
          user_key=_create_user_encrypted_key(project_id)
       return _decrypte_key(user_key,MASTER_KEY)
       
def _check_user_encrypted_key(project_id):
       #TODO Check in DB
       return None

def _create_user_encrypted_key(project_id):
       user_key=_create_new_user_key()
       encrypted_key=_encrypt_key(user_key,MASTER_KEY)
       #TODO save in db
       return encrypted_key


def _create_new_volume_encrypted_key(user_key):
       volume_key=_create_new_volume_key()
       encrypted_key=_encrypt_key(volume_key,user_key)
       return encrypted_key

def _create_new_volume_key():
       #TODO create volume key
       return "Volume_Key" 

def _create_new_user_key():
       #TODO create user key
       return "USER_Key"

def _encrypt_key(key,encryption_key):
       #TODO do encryption
       return key

def _decrypte_key(key,encryption_key):
       #TODO do decryption
       return key
     

