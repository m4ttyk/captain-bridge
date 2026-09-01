from __future__ import annotations
import json, os, shutil, tempfile
from pathlib import Path
from .domain import ConflictError, NotFoundError, ValidationError

class Storage:
    def __init__(self, home_dir: str|Path|None=None):
        self.home_dir = Path(home_dir or os.environ.get('CAPTAIN_BRIDGE_HOME', '~/.captain-bridge')).expanduser().resolve()
        self.ships_dir = self.home_dir / 'ships'
    def ensure_defaults(self) -> None:
        self.home_dir.mkdir(parents=True, exist_ok=True); self.ships_dir.mkdir(exist_ok=True)
        root = Path(__file__).parent / 'resources'
        for src in [root/'authority.md', *sorted((root/'roles').glob('*.md'))]:
            dst = self.home_dir / ('roles/'+src.name if src.parent.name == 'roles' else src.name)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists(): shutil.copyfile(src, dst)
    def atomic_write_text(self, path: str|Path, text: str) -> Path:
        path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        fd,tmp=tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f: f.write(text); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        return path
    def atomic_write_json(self,path,obj): return self.atomic_write_text(path,json.dumps(obj,indent=2,sort_keys=True)+'\n')
    def read_json(self,path):
        try: return json.loads(Path(path).read_text())
        except FileNotFoundError: raise NotFoundError(f'not found: {path}')
        except json.JSONDecodeError as e: raise ValidationError(f'invalid JSON: {path}') from e
    def exclusive_write_json(self,path,obj):
        path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                f.write(json.dumps(obj,indent=2,sort_keys=True)+'\n'); f.flush(); os.fsync(f.fileno())
            try: os.link(tmp, path)
            except FileExistsError: raise ConflictError(f'already exists: {path}')
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        return path
    def resolve_ship(self, explicit=None):
        candidates=[]
        if explicit: candidates.append(Path(explicit))
        if os.environ.get('CAPTAIN_BRIDGE_SHIP'): candidates.append(Path(os.environ['CAPTAIN_BRIDGE_SHIP']))
        p=Path.cwd(); candidates.extend([p,*p.parents])
        for c in candidates:
            c=c.expanduser().resolve()
            if (c/'metadata.json').exists() and (c/'index.md').exists(): return c
        raise NotFoundError('ship not found')
    def append_event(self, ship, event):
        path=Path(ship)/'events'; path.mkdir(parents=True,exist_ok=True)
        event_id=event.get('id')
        if not event_id:
            event_id=__import__('captain_bridge.domain',fromlist=['new_id']).new_id('event')
            event['id']=event_id
        self.exclusive_write_json(path/f'{event_id}.json', event)
