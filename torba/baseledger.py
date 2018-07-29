import os
import logging
from binascii import hexlify, unhexlify
from typing import Dict, Type, Iterable
from operator import itemgetter
from collections import namedtuple

from twisted.internet import defer

from torba import baseaccount
from torba import basedatabase
from torba import baseheader
from torba import basenetwork
from torba import basetransaction
from torba.coinselection import CoinSelector
from torba.constants import COIN, NULL_HASH32
from torba.stream import StreamController
from torba.hash import hash160, double_sha256, sha256, Base58

log = logging.getLogger(__name__)

LedgerType = Type['BaseLedger']


class LedgerRegistry(type):

    ledgers: Dict[str, LedgerType] = {}

    def __new__(mcs, name, bases, attrs):
        cls: LedgerType = super().__new__(mcs, name, bases, attrs)
        if not (name == 'BaseLedger' and not bases):
            ledger_id = cls.get_id()
            assert ledger_id not in mcs.ledgers,\
                'Ledger with id "{}" already registered.'.format(ledger_id)
            mcs.ledgers[ledger_id] = cls
        return cls

    @classmethod
    def get_ledger_class(mcs, ledger_id: str) -> LedgerType:
        return mcs.ledgers[ledger_id]


class TransactionEvent(namedtuple('TransactionEvent', ('address', 'tx', 'height', 'is_verified'))):
    pass


class BaseLedger(metaclass=LedgerRegistry):

    name: str
    symbol: str
    network_name: str

    account_class = baseaccount.BaseAccount
    database_class = basedatabase.BaseDatabase
    headers_class = baseheader.BaseHeaders
    network_class = basenetwork.BaseNetwork
    transaction_class = basetransaction.BaseTransaction

    secret_prefix = None
    pubkey_address_prefix: bytes
    script_address_prefix: bytes
    extended_public_key_prefix: bytes
    extended_private_key_prefix: bytes

    default_fee_per_byte = 10

    def __init__(self, config=None):
        self.config = config or {}
        self.db = self.config.get('db') or self.database_class(
            os.path.join(self.path, "blockchain.db")
        )  # type: basedatabase.BaseDatabase
        self.network = self.config.get('network') or self.network_class(self)
        self.network.on_header.listen(self.process_header)
        self.network.on_status.listen(self.process_status)
        self.accounts = []
        self.headers = self.config.get('headers') or self.headers_class(self)
        self.fee_per_byte: int = self.config.get('fee_per_byte', self.default_fee_per_byte)

        self._on_transaction_controller = StreamController()
        self.on_transaction = self._on_transaction_controller.stream
        self.on_transaction.listen(
            lambda e: log.info(
                '(%s) on_transaction: address=%s, height=%s, is_verified=%s, tx.id=%s',
                self.get_id(), e.address, e.height, e.is_verified, e.tx.id
            )
        )

        self._on_header_controller = StreamController()
        self.on_header = self._on_header_controller.stream

        self._transaction_processing_locks = {}
        self._utxo_reservation_lock = defer.DeferredLock()
        self._header_processing_lock = defer.DeferredLock()

    @classmethod
    def get_id(cls):
        return '{}_{}'.format(cls.symbol.lower(), cls.network_name.lower())

    @classmethod
    def hash160_to_address(cls, h160):
        raw_address = cls.pubkey_address_prefix + h160
        return Base58.encode(bytearray(raw_address + double_sha256(raw_address)[0:4]))

    @staticmethod
    def address_to_hash160(address):
        return Base58.decode(address)[1:21]

    @classmethod
    def public_key_to_address(cls, public_key):
        return cls.hash160_to_address(hash160(public_key))

    @staticmethod
    def private_key_to_wif(private_key):
        return b'\x1c' + private_key + b'\x01'

    @property
    def path(self):
        return os.path.join(self.config['data_path'], self.get_id())

    def get_input_output_fee(self, io: basetransaction.InputOutput) -> int:
        """ Fee based on size of the input / output. """
        return self.fee_per_byte * io.size

    def get_transaction_base_fee(self, tx):
        """ Fee for the transaction header and all outputs; without inputs. """
        return self.fee_per_byte * tx.base_size

    @defer.inlineCallbacks
    def add_account(self, account: baseaccount.BaseAccount) -> defer.Deferred:
        self.accounts.append(account)
        if self.network.is_connected:
            yield self.update_account(account)

    @defer.inlineCallbacks
    def get_transaction(self, txhash):
        raw, _, _ = yield self.db.get_transaction(txhash)
        if raw is not None:
            defer.returnValue(self.transaction_class(raw))

    @defer.inlineCallbacks
    def get_private_key_for_address(self, address):
        match = yield self.db.get_address(address)
        if match:
            for account in self.accounts:
                if match['account'] == account.public_key.address:
                    defer.returnValue(account.get_private_key(match['chain'], match['position']))

    @defer.inlineCallbacks
    def get_effective_amount_estimators(self, funding_accounts: Iterable[baseaccount.BaseAccount]):
        estimators = []
        for account in funding_accounts:
            utxos = yield account.get_unspent_outputs()
            for utxo in utxos:
                estimators.append(utxo.get_estimator(self))
        defer.returnValue(estimators)

    @defer.inlineCallbacks
    def get_spendable_utxos(self, amount: int, funding_accounts):
        yield self._utxo_reservation_lock.acquire()
        try:
            txos = yield self.get_effective_amount_estimators(funding_accounts)
            selector = CoinSelector(
                txos, amount,
                self.get_input_output_fee(
                    self.transaction_class.output_class.pay_pubkey_hash(COIN, NULL_HASH32)
                )
            )
            spendables = selector.select()
            if spendables:
                yield self.reserve_outputs(s.txo for s in spendables)
        except Exception:
            log.exception('Failed to get spendable utxos:')
            raise
        finally:
            self._utxo_reservation_lock.release()
        defer.returnValue(spendables)

    def reserve_outputs(self, txos):
        return self.db.reserve_outputs(txos)

    def release_outputs(self, txos):
        return self.db.release_outputs(txos)

    @defer.inlineCallbacks
    def get_local_status(self, address):
        address_details = yield self.db.get_address(address)
        history = address_details['history'] or ''
        h = sha256(history.encode())
        defer.returnValue(hexlify(h))

    @defer.inlineCallbacks
    def get_local_history(self, address):
        address_details = yield self.db.get_address(address)
        history = address_details['history'] or ''
        parts = history.split(':')[:-1]
        defer.returnValue(list(zip(parts[0::2], map(int, parts[1::2]))))

    @staticmethod
    def get_root_of_merkle_tree(branches, branch_positions, working_branch):
        for i, branch in enumerate(branches):
            other_branch = unhexlify(branch)[::-1]
            other_branch_on_left = bool((branch_positions >> i) & 1)
            if other_branch_on_left:
                combined = other_branch + working_branch
            else:
                combined = working_branch + other_branch
            working_branch = double_sha256(combined)
        return hexlify(working_branch[::-1])

    @defer.inlineCallbacks
    def is_valid_transaction(self, tx, height):
        height <= len(self.headers) or defer.returnValue(False)
        merkle = yield self.network.get_merkle(tx.id, height)
        merkle_root = self.get_root_of_merkle_tree(merkle['merkle'], merkle['pos'], tx.hash)
        header = self.headers[height]
        defer.returnValue(merkle_root == header['merkle_root'])

    @defer.inlineCallbacks
    def start(self):
        if not os.path.exists(self.path):
            os.mkdir(self.path)
        yield self.db.start()
        first_connection = self.network.on_connected.first
        self.network.start()
        yield first_connection
        self.headers.touch()
        yield self.update_headers()
        yield self.network.subscribe_headers()
        yield self.update_accounts()

    @defer.inlineCallbacks
    def stop(self):
        yield self.network.stop()
        yield self.db.stop()

    @defer.inlineCallbacks
    def update_headers(self):
        while True:
            height_sought = len(self.headers)
            headers = yield self.network.get_headers(height_sought, 2000)
            if headers['count'] <= 0:
                break
            yield self.headers.connect(height_sought, unhexlify(headers['hex']))
            self._on_header_controller.add(self.headers.height)

    @defer.inlineCallbacks
    def process_header(self, response):
        yield self._header_processing_lock.acquire()
        try:
            header = response[0]
            if header['height'] == len(self.headers):
                # New header from network directly connects after the last local header.
                yield self.headers.connect(len(self.headers), unhexlify(header['hex']))
                self._on_header_controller.add(self.headers.height)
            elif header['height'] > len(self.headers):
                # New header is several heights ahead of local, do download instead.
                yield self.update_headers()
        finally:
            self._header_processing_lock.release()

    def update_accounts(self):
        return defer.DeferredList([
            self.update_account(a) for a in self.accounts
        ])

    @defer.inlineCallbacks
    def update_account(self, account):  # type: (baseaccount.BaseAccount) -> defer.Defferred
        # Before subscribing, download history for any addresses that don't have any,
        # this avoids situation where we're getting status updates to addresses we know
        # need to update anyways. Continue to get history and create more addresses until
        # all missing addresses are created and history for them is fully restored.
        yield account.ensure_address_gap()
        addresses = yield account.get_addresses(max_used_times=0)
        while addresses:
            yield defer.DeferredList([
                self.update_history(a) for a in addresses
            ])
            addresses = yield account.ensure_address_gap()

        # By this point all of the addresses should be restored and we
        # can now subscribe all of them to receive updates.
        all_addresses = yield account.get_addresses()
        yield defer.DeferredList(
            list(map(self.subscribe_history, all_addresses))
        )

    @defer.inlineCallbacks
    def update_history(self, address):
        remote_history = yield self.network.get_history(address)
        local_history = yield self.get_local_history(address)

        synced_history = []
        for i, (hex_id, remote_height) in enumerate(map(itemgetter('tx_hash', 'height'), remote_history)):

            synced_history.append((hex_id, remote_height))

            if i < len(local_history) and local_history[i] == (hex_id, remote_height):
                continue

            lock = self._transaction_processing_locks.setdefault(hex_id, defer.DeferredLock())

            yield lock.acquire()

            try:

                # see if we have a local copy of transaction, otherwise fetch it from server
                raw, _, is_verified = yield self.db.get_transaction(hex_id)
                save_tx = None
                if raw is None:
                    _raw = yield self.network.get_transaction(hex_id)
                    tx = self.transaction_class(unhexlify(_raw))
                    save_tx = 'insert'
                else:
                    tx = self.transaction_class(raw)

                if remote_height > 0 and not is_verified:
                    is_verified = yield self.is_valid_transaction(tx, remote_height)
                    is_verified = 1 if is_verified else 0
                    if save_tx is None:
                        save_tx = 'update'

                yield self.db.save_transaction_io(
                    save_tx, tx, remote_height, is_verified, address, self.address_to_hash160(address),
                    ''.join('{}:{}:'.format(tx_id, tx_height) for tx_id, tx_height in synced_history)
                )

                log.debug(
                    "%s: sync'ed tx %s for address: %s, height: %s, verified: %s",
                    self.get_id(), hex_id, address, remote_height, is_verified
                )

                self._on_transaction_controller.add(TransactionEvent(address, tx, remote_height, is_verified))

            except Exception:
                log.exception('Failed to synchronize transaction:')
                raise

            finally:
                lock.release()
                if not lock.locked and hex_id in self._transaction_processing_locks:
                    del self._transaction_processing_locks[hex_id]

    @defer.inlineCallbacks
    def subscribe_history(self, address):
        remote_status = yield self.network.subscribe_address(address)
        local_status = yield self.get_local_status(address)
        if local_status != remote_status:
            yield self.update_history(address)

    @defer.inlineCallbacks
    def process_status(self, response):
        address, remote_status = response
        local_status = yield self.get_local_status(address)
        if local_status != remote_status:
            yield self.update_history(address)

    def broadcast(self, tx):
        return self.network.broadcast(hexlify(tx.raw).decode())
