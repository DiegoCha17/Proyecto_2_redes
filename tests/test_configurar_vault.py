"""Configuración de Vault sin red y únicamente con secretos sintéticos."""
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import configurar_vault as configurador
from scripts.ejecutar_con_vault import ErrorVault, credenciales_vault

DATOS = {'r1': {'usuario': 'test_r1', 'clave': 'aB9!x'},
         'sw1': {'usuario': 'test_sw1', 'clave': 'otro#123'}}


def test_pide_confirmacion_y_rechaza_datos_distintos():
    with patch.object(configurador.getpass, 'getpass', side_effect=['primero', 'distinto']):
        with pytest.raises(ErrorVault, match='no coinciden'):
            configurador._pedir_dato('Contraseña de R1')


def test_cancelacion_no_cifra_ni_sustituye(tmp_path, capsys):
    maestra = tmp_path / 'master'
    maestra.write_text('clave-maestra-sintetica\n')
    vault = tmp_path / 'vault.yml'
    original = b'$ANSIBLE_VAULT;1.1;AES256\noriginal-cifrado\n'
    vault.write_bytes(original)
    with patch.object(configurador, 'VAULT', vault), \
         patch.dict(os.environ, {'ANSIBLE_VAULT_PASSWORD_FILE': str(maestra)}, clear=True), \
         patch.object(configurador.getpass, 'getpass', side_effect=['test_r1', 'otro']):
        assert configurador.main() == 1
    assert vault.read_bytes() == original
    assert not list(tmp_path.glob('vault.yml.bak-*'))
    assert 'test_r1' not in capsys.readouterr().out


def test_verificacion_fallida_no_sustituye_vault(tmp_path):
    maestra = tmp_path / 'master'
    maestra.write_text('clave-maestra-sintetica\n')
    vault = tmp_path / 'vault.yml'
    original = b'$ANSIBLE_VAULT;1.1;AES256\noriginal-cifrado\n'
    vault.write_bytes(original)
    env = {'ANSIBLE_VAULT_PASSWORD_FILE': str(maestra)}
    def cifrar(comando, **kwargs):
        assert 'ANSIBLE_VAULT_PASSWORD_FILE' not in kwargs['env']
        Path(comando[comando.index('--output') + 1]).write_bytes(b'$ANSIBLE_VAULT;1.1;AES256\nnuevo-cifrado\n')
        return SimpleNamespace(returncode=0)
    with patch.object(configurador.subprocess, 'run', side_effect=cifrar), \
         patch.object(configurador, 'credenciales_vault', return_value={'incorrecto': 'valor'}):
        with pytest.raises(ErrorVault, match='comprobación exacta'):
            configurador.configurar(vault, str(maestra), DATOS, env)
    assert vault.read_bytes() == original
    assert not list(tmp_path.glob('vault.yml.bak-*'))


@pytest.mark.skipif(not shutil.which('ansible-vault'), reason='Ansible Vault no instalado')
def test_reconstruccion_real_verifica_con_lanzador_y_conserva_respaldo(tmp_path):
    maestra = tmp_path / 'master'
    maestra.write_text('clave-maestra-sintetica\n')
    ruta = tmp_path / 'vault.yml'
    env = os.environ.copy()
    env['ANSIBLE_LOCAL_TEMP'] = str(tmp_path)
    env['ANSIBLE_VAULT_PASSWORD_FILE'] = str(maestra)
    configurador.configurar(ruta, str(maestra), DATOS, env)
    anterior = ruta.read_bytes()
    assert anterior.startswith(b'$ANSIBLE_VAULT;')
    nuevos = {'r1': {'usuario': 'admin', 'clave': '12345'},
              'sw1': {'usuario': 'otro', 'clave': 'xY8#z'}}
    respaldo = configurador.configurar(ruta, str(maestra), nuevos, env)
    assert respaldo.read_bytes() == anterior
    assert oct(respaldo.stat().st_mode & 0o777) == '0o600'
    assert oct(ruta.stat().st_mode & 0o777) == '0o600'
    assert credenciales_vault(ruta, env) == {
        'NETAGENT_R1_USUARIO': 'admin', 'NETAGENT_R1_CLAVE': '12345',
        'NETAGENT_SW1_USUARIO': 'otro', 'NETAGENT_SW1_CLAVE': 'xY8#z'}
    assert b'12345' not in ruta.read_bytes()


@pytest.mark.skipif(not shutil.which('ansible-vault'), reason='Ansible Vault no instalado')
def test_configurador_con_clave_ssh_igual_a_maestra(tmp_path):
    maestra = tmp_path / 'master'
    maestra.write_text('pru42\n')
    ruta = tmp_path / 'vault.yml'
    env = os.environ.copy()
    env['ANSIBLE_LOCAL_TEMP'] = str(tmp_path)
    env['ANSIBLE_VAULT_PASSWORD_FILE'] = str(maestra)
    datos = {'r1': {'usuario': 'admin', 'clave': 'pru42'},
             'sw1': {'usuario': 'admin', 'clave': 'adminpru42'}}
    assert configurador.configurar(ruta, str(maestra), datos, env) is None
    resultado = credenciales_vault(ruta, env)
    assert resultado['NETAGENT_R1_CLAVE'] == datos['r1']['clave']
    assert resultado['NETAGENT_SW1_CLAVE'] == datos['sw1']['clave']
