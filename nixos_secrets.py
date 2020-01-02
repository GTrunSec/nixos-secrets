#!/usr/bin/env python3

import argparse
import collections
import fnmatch
import json
import logging
import os
import subprocess
import sys
import tempfile
from operator import itemgetter
from typing import Generator, Dict, Set, Any, List, Deque, Optional, Iterable, Union, BinaryIO

import gnupg  # type: ignore

logging.basicConfig()

logger = logging.getLogger("nixos_secrets")
logger.setLevel(logging.DEBUG)

EXCLUDE_FILES = [
    '*.nix',
    '.git/*',
    '.git',
    '.pre-commit'
]

gpg = gnupg.GPG()


def get_umask() -> int:
    old_umask = os.umask(0)
    os.umask(old_umask)
    return old_umask


def parse_nix(file_path: str) -> Dict[str, Any]:
    return json.loads(subprocess.check_output(
        ["nix-instantiate", "--json", "--strict", "--eval", file_path]))


def wrap_string_list(iter_or_str: Union[str, Iterable[str]]) -> Iterable[str]:
    """
    If passed a string, wrap it in an iterable. Otherwise, return the passed
    in value.
    :param iter_or_str: a string or iterable of strings
    :return: an iterable of strings
    """
    if isinstance(iter_or_str, str):
        return iter_or_str,
    else:
        return iter_or_str


class SecretKeyError(Exception):
    pass


class SecretError(Exception):
    def __init__(self, path: str, message: str):
        super(SecretError, self).__init__(f"{path}: {message}")


# Based on https://github.com/isislovecruft/python-gnupg/blob/master/pretty_bad_protocol/_parsers.py
class ListPackets:
    """Handle status messages for --list-packets."""

    def __init__(self, gpg: gnupg.GPG):
        self._gpg = gpg
        #: True if the passphrase to a public/private keypair is required.
        self.need_passphrase: Optional[bool] = None
        #: True if a passphrase for a symmetric key is required.
        self.need_passphrase_sym: Optional[bool] = None
        #: The keyid and uid which this data is encrypted to.
        self.userid_hint: Optional[List[str]] = None
        #: A list of keyid's that the message has been encrypted to.
        self.encrypted_to: List[str] = []

        # Set by internal gnupg code
        self.data: Optional[bytes] = None
        self.stderr: Optional[str] = None

    def handle_status(self, key: str, value: str):
        """Parse a status code from the attached GnuPG process."""

        if key == 'ENC_TO':
            key, _, _ = value.split()
            self.encrypted_to.append(key)
        elif key in ('NEED_PASSPHRASE', 'MISSING_PASSPHRASE'):
            self.need_passphrase = True
        elif key == 'NEED_PASSPHRASE_SYM':
            self.need_passphrase_sym = True
        elif key == 'USERID_HINT':
            self.userid_hint = value.strip().split()
        else:
            gnupg.logger.debug('message ignored: %s, %s', key, value)


class Secret:
    _logger = logger.getChild('secret')

    def __init__(self, path: str, keys: Set[str]):
        self._path = path
        self._keys = keys
        self._encrypted: Optional[bool] = None

    def _detect_encryption(self) -> bool:
        """
        Try to detect a PGP encrypted file that was generated by this script.
        Checks the first packet type and encryption algorithm.
        :return: True if the file seems to be an encrypted secret
        """
        with open(self._path, mode='rb') as file:
            header = file.read(32)

            try:
                # Check that first bit is 1
                if header[0] & 0x80 == 0:
                    return False

                if header[0] & 0x40 == 0:
                    # Old packet format

                    # Check that packet tag is 1: "Public-Key Encrypted Session Key Packet"
                    if (header[0] & 0x3C) >> 2 != 1:
                        return False

                    length_type = header[0] & 0x3
                    if length_type == 0:
                        body_offset = 2
                    elif length_type == 1:
                        body_offset = 3
                    elif length_type == 2:
                        body_offset = 5
                    else:  # if length_type == 3:
                        # Indeterminate length
                        body_offset = 1

                    # Version 3
                    if header[body_offset] != 3:
                        return False

                else:
                    # New packet format

                    # Check that packet tag is 1: "Public-Key Encrypted Session Key Packet"
                    if header[0] & 0x3F != 1:
                        return False

                    # More checks could be done, but GPG doesn't even seem to generate this format
            except IndexError:
                # Packet was too short
                return False
            return True

    def _list_packets(self) -> ListPackets:
        """List the packet contents of this secret."""
        args = ['--list-packets', '--pinentry-mode', 'cancel']
        result = ListPackets(gpg)
        with open(self._path, 'rb') as file:
            gpg._handle_io(args, file, result, binary=True)
        return result

    @staticmethod
    def _get_master_keys(keys: Iterable[str]) -> Set[str]:
        """
        Convert a list of key IDs or fingerprints to a set of the unique master
        key fingerprints corresponding to the input.

        :param keys: input key IDs or fingerprints
        :return: set of unique key fingerprints
        """
        key_list: gnupg.ListKeys = gpg.list_keys(keys=keys)
        return set(map(itemgetter('fingerprint'), key_list))

    @property
    def encrypted(self) -> bool:
        if self._encrypted is not None:
            return self._encrypted
        self._encrypted = self._detect_encryption()
        return self._encrypted

    def update_keys(self):
        current_keys = set()
        if self.encrypted:
            packets = self._list_packets()
            current_keys = self._get_master_keys(packets.encrypted_to)
        if current_keys != self._keys:
            self._logger.debug(f"Adding keys: {self._keys - current_keys}, removing keys: {current_keys - self._keys}")
            if self.encrypted:
                self.decrypt()
            self.encrypt()
        else:
            self._logger.debug("Keys are up to date")

    def encrypt(self) -> None:
        if self.encrypted:
            self._logger.warning(f"File is already encrypted: {self._path}")
            return

        file_dir, file_name = os.path.split(self._path)
        with open(self._path, 'rb') as file, \
                tempfile.NamedTemporaryFile(dir=file_dir, prefix=file_name,
                                            delete=False) as enc_temp:  # type: BinaryIO, Any
            result: gnupg.Crypt = gpg.encrypt_file(file, output=enc_temp.name, recipients=self._keys,
                                                   armor=False, always_trust=True)
            if result.ok:
                os.fchmod(enc_temp.file.fileno(), 0o666 & ~get_umask())
        if result.ok:
            os.replace(enc_temp.name, self._path)
            self._encrypted = True
        else:
            os.remove(enc_temp.name)
            raise SecretError(self._path, result.status)

    def decrypt(self) -> None:
        file_dir, file_name = os.path.split(self._path)
        with open(self._path, 'rb') as file, \
                tempfile.NamedTemporaryFile(dir=file_dir, prefix=file_name,
                                            delete=False) as dec_temp:  # type: BinaryIO, Any
            result = gpg.decrypt_file(file, output=dec_temp.name)
            if result.ok:
                os.fchmod(dec_temp.file.fileno(), 0o666 & ~get_umask())
        if result.ok:
            os.replace(dec_temp.name, self._path)
            self._encrypted = False
        else:
            os.remove(dec_temp.name)
            raise SecretError(self._path, result.status)


class KeyGenerator:
    def __init__(self, config: Dict[str, Any]):
        self._key_type: Optional[str] = config.get('keyType', 'RSA')
        self._key_length: Optional[int] = config.get('keyLength', 4096)
        self._domain: str = config['domain']

    def generate(self, name: str, key_path: Optional[str] = None) -> gnupg.GenKey:
        if not key_path:
            key_path = f'{name}.asc'

        key_input = "%no-protection\n" + gpg.gen_key_input(
            key_type=self._key_type,
            key_length=self._key_length,
            name_real=name,
            name_email=f'{name.lower()}@{self._domain}',
            passphrase='',
            expire_date=0)
        key: gnupg.GenKey = gpg.gen_key(key_input)
        if not key:
            raise SecretKeyError(f"Failed to generate key: {key}")
        with open(key_path, 'w') as key_file:
            key_data = gpg.export_keys(key.fingerprint, secret=True, passphrase='')
            key_file.write(key_data)
        gpg.delete_keys(key.fingerprint, secret=True, passphrase='')
        return key


class KeyManager:
    def __init__(self, config: Dict[str, Union[str, Iterable[str]]]):
        self._aliases: Dict[str, Set[str]] = {alias: set(wrap_string_list(key_config))
                                              for (alias, key_config) in config.items()}
        self._aliases['all'] = set.union(*self._aliases.values())

    def lookup_alias(self, alias: str) -> Set[str]:
        try:
            return self._aliases[alias]
        except KeyError as e:
            raise SecretKeyError(f"unknown key alias: {alias}") from e


class SecretsConfig:
    def __init__(self, config_path: str):
        self._path = config_path

        if os.path.isdir(self._path):
            self.dir = self._path
        else:
            self.dir = os.path.dirname(self._path)

        config_data = parse_nix(self._path)

        self.key_generator = KeyGenerator(config_data.get('generate', {}))
        self.key_manager = KeyManager(config_data.get('keys', {}))

        self._secrets: List[Secret] = []
        self._path_secrets: Dict[str, Secret] = {}
        parse_queue: Deque[Dict[str, Any]] = collections.deque((config_data['secrets'],))
        while parse_queue:
            data = parse_queue.pop()
            path: Optional[str] = data.pop('path', None)
            key_aliases: List[str] = data.pop('keys', []) + ['master']
            keys = set.union(*map(self.key_manager.lookup_alias, key_aliases))
            if path and keys:
                secret = Secret(path, keys)
                self._secrets.append(secret)
                self._path_secrets[path] = secret

            # All other attributes are child secrets
            for _, cd in data.items():
                if isinstance(cd, str):
                    child_data: Dict = {'path': cd}
                else:
                    child_data = cd
                child_data.setdefault('keys', []).extend(key_aliases)
                parse_queue.appendleft(child_data)

    def lookup_path(self, path: str) -> Secret:
        try:
            return self._path_secrets[path]
        except KeyError as e:
            raise SecretError(path, "secret path is not configured") from e

    def is_excluded(self, path: str):
        path = os.path.relpath(path, self.dir)
        for pattern in EXCLUDE_FILES:
            if fnmatch.fnmatch(path, pattern):
                return True
        return False

    def all_secrets(self) -> Generator[str, None, None]:
        """
        Generate the paths of all the possible secrets in the directory. This
        includes files that are not specified in the config.
        """
        for dir_path, dir_names, file_names in os.walk(self.dir):
            for i, dir_name in enumerate(dir_names):
                if self.is_excluded(os.path.join(dir_path, dir_name)):
                    del dir_names[i]

            for file_name in file_names:
                file_path = os.path.join(dir_path, file_name)
                if self.is_excluded(file_path):
                    continue
                yield file_path


def encrypt_command(config: SecretsConfig, args: argparse.Namespace) -> int:
    def encrypt_path(path: str):
        path = os.path.relpath(path, config.dir)
        secret = config.lookup_path(path)
        secret.update_keys()

    for arg_path in args.files:
        if os.path.isdir(arg_path) and args.recursive:
            for dirpath, _, filenames in os.walk(arg_path):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    if config.is_excluded(path):
                        continue
                    encrypt_path(path)
        else:
            encrypt_path(arg_path)
    return 0


def decrypt_command(config: SecretsConfig, args: argparse.Namespace) -> int:
    for path in args.files:
        secret = Secret(path, set())
        secret.decrypt()
    return 0


def check_command(config, args: argparse.Namespace) -> int:
    # Check all files, even if they are not in the config file
    # This prevents committing unencrypted files
    all_encrypted = True
    for path in config.all_secrets():
        secret = Secret(path, set())
        if not secret.encrypted:
            print("\"{}\" is not encrypted".format(path))
            all_encrypted = False
    if all_encrypted:
        print("All secrets are encrypted")
    else:
        return 2
    return 0


def generate_command(config: SecretsConfig, args: argparse.Namespace) -> int:
    key = config.key_generator.generate(args.name, args.key_file)
    print(key.fingerprint)
    return 0


def check_fd(value: str) -> int:
    try:
        fd = int(value)
        if fd < 0:
            raise ValueError()
    except ValueError:
        raise argparse.ArgumentTypeError("file descriptor must be an integer >= 0")
    return fd


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage NixOS secret files")
    parser.add_argument('-c', '--config', default=os.getcwd(), help="Nix configuration file for secrets")
    subparsers = parser.add_subparsers()

    encrypt_parser = subparsers.add_parser('encrypt', help="encrypt secrets or update keys", description='''
        Encrypt the specified files using the keys specified in the config
        file. If keys need to be added or removed, the master private key
        will be required.
    ''')
    encrypt_parser.add_argument('files', nargs='+', help="files to encrypt")
    encrypt_parser.add_argument('-r', '--recursive', action='store_true',
                                help='recursively encrypt specified directories')
    encrypt_parser.set_defaults(func=encrypt_command)

    decrypt_parser = subparsers.add_parser('decrypt', help="decrypt secrets")
    decrypt_parser.add_argument('files', nargs='+', help='files to decrypt')
    decrypt_parser.set_defaults(func=decrypt_command)

    generate_parser = subparsers.add_parser('generate', help="generate PGP key", description='''
        Generate a PGP key designed to be used to encrypt NixOS secrets. This
        command will generate a key with no passphrase. The private key will be
        exported to a file specified by the --key-file option, and the public
        key will remain in the keyring.
    ''')
    generate_parser.add_argument('name', help='name of the key')
    generate_parser.add_argument('--key-file', help='file name for exported secret key')
    generate_parser.set_defaults(func=generate_command)

    check_parser = subparsers.add_parser('check', help="check that all secrets are encrypted")
    check_parser.add_argument('dir', nargs='?', default=os.getcwd(), help='directory to scan')
    check_parser.set_defaults(func=check_command)

    args = parser.parse_args()

    if 'func' in args:
        config = SecretsConfig(args.config)
        return args.func(config, args)
    else:
        parser.print_usage()
        return 1


if __name__ == '__main__':
    sys.exit(main())
