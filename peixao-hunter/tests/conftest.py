"""Isola os testes do diretório de dados real.

``Settings`` lê o ambiente na importação, então o diretório temporário precisa
estar definido antes de qualquer ``import peixao``.
"""
import os
import tempfile

os.environ.setdefault("PEIXAO_DATA_DIR", tempfile.mkdtemp(prefix="peixao-tests-"))
