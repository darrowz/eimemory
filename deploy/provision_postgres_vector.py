"""Provision a dedicated localhost pgvector database; never enable live recall.

Run as the eimemory service account with sudo available, after installing
PostgreSQL and its pgvector package. Refuses to overwrite existing resources.
The embedding model/dimension and index migration are a separate checked step.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import subprocess
import tempfile


def sql(statement):
    result = subprocess.run(['sudo', '-n', '-u', 'postgres', 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-At'],
                            input=statement, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('postgres_provisioning_sql_failed')
    return result.stdout.strip()


def main():
    target = Path('/etc/eimemory/postgres.env')
    if target.exists() or target.is_symlink():
        raise RuntimeError('postgres_configuration_already_exists')
    if sql("SELECT 1 FROM pg_roles WHERE rolname='eimemory_vector'"):
        raise RuntimeError('postgres_role_already_exists')
    if sql("SELECT 1 FROM pg_database WHERE datname='eimemory_vector'"):
        raise RuntimeError('postgres_database_already_exists')
    password = secrets.token_hex(32)
    sql(f"CREATE ROLE eimemory_vector LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '{password}';")
    sql('CREATE DATABASE eimemory_vector OWNER eimemory_vector;')
    sql('REVOKE ALL ON DATABASE eimemory_vector FROM PUBLIC;')
    sql('\\connect eimemory_vector\nCREATE EXTENSION vector;\nREVOKE CREATE ON SCHEMA public FROM PUBLIC;')
    configuration = ('# Prepared only: enable after embedding and index validation.\n'
                     'EIMEMORY_POSTGRES_VECTOR_ENABLED=0\n'
                     f'EIMEMORY_POSTGRES_VECTOR_DSN=postgresql://eimemory_vector:{password}@127.0.0.1:5432/eimemory_vector\n')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(configuration)
        subprocess.run(['sudo', '-n', 'install', '-m', '600', '-o', str(os.getuid()), '-g', str(os.getgid()),
                        str(temporary), str(target)], check=True, capture_output=True)
        result = subprocess.run(['psql', '-X', '-h', '127.0.0.1', '-U', 'eimemory_vector', '-d', 'eimemory_vector',
                                 '-Atc', "SELECT extversion FROM pg_extension WHERE extname='vector'"],
                                env={**os.environ, 'PGPASSWORD':password}, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('postgres_application_login_failed')
        print('dedicated_database=ready pgvector='+result.stdout.strip()+' runtime_enabled=false')
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
