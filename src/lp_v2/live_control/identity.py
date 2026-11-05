"""Business account equality; operation/profile bindings remain byte-exact."""
import re


def same_identity(left,right):
    fields={'signer_address','wallet_address','signature_type'}
    for value in (left,right):
        if (not isinstance(value,dict) or set(value)!=fields
                or type(value['signature_type']) is not int or value['signature_type']!=1
                or any(not isinstance(value[key],str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}',value[key])
                       for key in ('signer_address','wallet_address'))):
            return False
    return all(left[key].lower()==right[key].lower() for key in ('signer_address','wallet_address'))


def matching_tail(value,address):
    return (isinstance(value,str) and re.fullmatch(r'[0-9a-fA-F]{4}',value) is not None
            and isinstance(address,str) and re.fullmatch(r'0x[0-9a-fA-F]{40}',address) is not None
            and value.lower()==address[-4:].lower())
