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


def single_market_tokens(identity,owner,market):
    """Accept only equivalent legacy or singleton M5 journal identities."""
    if not isinstance(identity,dict) or identity.get('owner')!=owner:
        raise ValueError('journal owner differs')
    if set(identity)=={'owner','condition_id','token_ids'}:
        if identity['condition_id']!=market:raise ValueError('journal market differs')
        tokens=identity['token_ids']
    elif set(identity)=={'owner','markets','at'}:
        from lp_v2.live_control.profiles import timestamp
        at=timestamp(identity['at'].isoformat())
        if at.utcoffset() is None:raise ValueError('journal identity time unavailable')
        markets=identity['markets']
        if not isinstance(markets,dict) or set(markets)!={market}:
            raise ValueError('singleton journal market required')
        tokens=markets[market]
    else:
        raise ValueError('journal identity schema unavailable')
    if (not isinstance(tokens,tuple) or len(tokens)!=2 or tokens[0]==tokens[1]
            or any(not isinstance(token,str) or not token for token in tokens)):
        raise ValueError('journal tokens unavailable')
    return tokens
