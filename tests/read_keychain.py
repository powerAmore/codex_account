import ctypes
import ctypes.util

# Load Security framework
sec_path = ctypes.util.find_library('Security')
sec = ctypes.CDLL(sec_path)

# Define types
sec.SecKeychainFindGenericPassword.argtypes = [
    ctypes.c_void_p,  # keychainOrArray
    ctypes.c_uint32,  # serviceNameLength
    ctypes.c_char_p,  # serviceName
    ctypes.c_uint32,  # accountNameLength
    ctypes.c_char_p,  # accountName
    ctypes.POINTER(ctypes.c_uint32),  # passwordLength
    ctypes.POINTER(ctypes.c_void_p),  # passwordData
    ctypes.c_void_p   # itemRef
]
sec.SecKeychainItemFreeContent.argtypes = [
    ctypes.c_void_p,  # attrList
    ctypes.c_void_p   # data
]

def get_raw_keychain_password(service, account):
    service_bytes = service.encode('utf-8')
    account_bytes = account.encode('utf-8')
    
    password_length = ctypes.c_uint32(0)
    password_data = ctypes.c_void_p(0)
    
    status = sec.SecKeychainFindGenericPassword(
        None,
        len(service_bytes),
        service_bytes,
        len(account_bytes),
        account_bytes,
        ctypes.byref(password_length),
        ctypes.byref(password_data),
        None
    )
    
    if status != 0:
        raise Exception(f"SecKeychainFindGenericPassword failed with status {status}")
        
    try:
        length = password_length.value
        data_ptr = password_data.value
        if not data_ptr:
            return b""
        raw_bytes = ctypes.string_at(data_ptr, length)
        return raw_bytes
    finally:
        sec.SecKeychainItemFreeContent(None, password_data)

def main():
    try:
        pw = get_raw_keychain_password("Codex Safe Storage", "Codex Key")
        print(f"Raw password bytes (length {len(pw)}): {pw.hex()}")
        try:
            print(f"Decoded UTF-8: {pw.decode('utf-8')}")
        except Exception:
            print("Could not decode as UTF-8")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
