"""Installed-package wrapper around the standalone reconnecting worker."""
import asyncio
from funguys.worker.runtime import WorkerRuntime

class TeammateWorkerNode:
    def __init__(self, host_url, owner_name, profile_key='codex-pro', initial_used_pct=0, work_dir=None, use_real_cli=True, token='', machine_name=''):
        if not use_real_cli:
            raise ValueError('Synthetic workers have been removed; use funguys simulate for demo artifacts')
        self.runtime = WorkerRuntime({'host_url':host_url,'name':owner_name,'profile':profile_key,'used_pct':initial_used_pct,'token':token,'codename':machine_name,**({'state_dir':str(work_dir)} if work_dir else {})})

    async def listen_forever(self):
        try:
            await asyncio.to_thread(self.runtime.run)
        finally:
            self.runtime.stop.set()
            self.runtime.cancelled.set()