"""External exchange simulator, constrained to the installed SDK's public API."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import create_autospec
from polymarket import SecureClient
from polymarket.models.clob.account import OpenOrder, ClobTrade, BalanceAllowance
from polymarket.models.clob.order_response import AcceptedOrder
from tests.test_live_sdk import AT, CID, WALLET, SIGNER, boundary


class Exchange:
    def __init__(self, *, condition_id=CID):
        self.condition_id=condition_id
        self.sdk = create_autospec(SecureClient, instance=True, spec_set=True)
        self.orders, self.trades, self.posts = {}, [], []
        self.cash, self.quantities = D('100'), {}
        self.cancel_unknown = False
        self.reject_tokens = set()
        self.now = AT
        self.sdk.create_limit_order.side_effect = self.sign
        self.sdk.post_order.side_effect = self.post
        self.sdk.get_order.side_effect = self.get_order
        self.sdk.list_open_orders.side_effect = lambda **kw: (SimpleNamespace(items=tuple(o for o in self.orders.values() if o.status == 'LIVE')),)
        self.sdk.list_account_trades.side_effect = lambda **kw: (SimpleNamespace(items=tuple(self.trades)),)
        self.sdk.get_balance_allowance.side_effect = self.balance
        self.sdk.cancel_order.side_effect = self.cancel
        self.sdk.get_closed_only_mode.return_value = False

    def get_order(self, *, order_id):
        from polymarket.errors import RequestRejectedError
        if order_id not in self.orders:
            raise RequestRejectedError("absent",status=404)
        return self.orders[order_id]

    def sign(self, **kw):
        return replace(boundary().create_limit_order.return_value, token_id=kw['token_id'],
            expiration=kw['expiration'], maker_amount=int(kw['price']*kw['size']*1000000),
            taker_amount=int(kw['size']*1000000), salt=len(self.posts)+1)

    def post(self, signed):
        from polymarket.models.clob.order_response import RejectedOrder
        self.posts.append(signed)
        if signed.token_id in self.reject_tokens:
            return RejectedOrder(code='post_only_would_cross',message='rejected')
        oid = f'order-{len(self.posts)}'
        self.orders[oid] = OpenOrder(id=oid, market=self.condition_id, asset_id=signed.token_id, owner='api-owner', maker_address=WALLET,
            side='BUY', price=str(D(signed.maker_amount)/signed.taker_amount), original_size=str(D(signed.taker_amount)/1000000),
            size_matched='0', outcome='Yes',order_type='GTD',status='LIVE',created_at=self.now,expiration=signed.expiration)
        return AcceptedOrder(order_id=oid,status='live',making_amount=D('0'),taking_amount=D('0'),trade_ids=(),transactions_hashes=())

    def balance(self, *, asset_type, token_id=None):
        amount = self.cash if asset_type == 'COLLATERAL' else self.quantities.get(token_id,D('0'))
        return BalanceAllowance(balance=int(amount*1000000), allowances={})

    def cancel(self, *, order_id):
        from polymarket.models.clob.cancel import CancelOrdersResponse
        if self.cancel_unknown:
            raise TimeoutError('unavailable')
        self.terminal(order_id)
        return CancelOrdersResponse(canceled=(order_id,),not_canceled={})

    def terminal(self, oid):
        self.orders[oid] = self.orders[oid].model_copy(update={'status':'CANCELED'})

    def fill(self, oid, quantity, price, *, at=None):
        o = self.orders[oid]
        quantity, price = D(quantity), D(price)
        self.now = at or self.now+timedelta(seconds=1)
        matched = o.size_matched+quantity
        self.orders[oid] = o.model_copy(update={'size_matched':matched,'status':'MATCHED' if matched == o.original_size else o.status})
        self.quantities[o.token_id] = self.quantities.get(o.token_id,D('0'))+quantity
        self.cash -= quantity*price
        self.trades.append(ClobTrade(id=f'trade-{len(self.trades)}',market=self.condition_id,asset_id=o.token_id,owner='api-owner',
            maker_address=WALLET,taker_order_id='foreign-taker',side='BUY',trader_side='MAKER',price=str(price),size=str(quantity),
            outcome='Yes',status='CONFIRMED',fee_rate_bps='0',bucket_index=0,transaction_hash='0x'+'b'*64,
            maker_orders=[dict(order_id=oid,asset_id=o.token_id,maker_address=WALLET,owner='api-owner',side='BUY',
                price=str(price),matched_amount=str(quantity),outcome='Yes',fee_rate_bps='0')],match_time=self.now,last_update=self.now))
