#!/usr/bin/env python3
"""Normalize loose media filenames without touching registered torrent content."""
import argparse
import fcntl
import os
from pathlib import Path
import re
import json
import subprocess
import difflib

VIDEO_EXT={'.mkv','.mp4','.avi','.m4v','.mov','.webm','.ts','.m2ts'}
AUDIO_EXT={'.mka','.aac','.ac3','.dts','.flac','.mp3','.m4a','.ogg','.wav'}

DEFAULT = Path(os.environ.get('MEDIA_ROOT','/srv/media'))
BACKUP = Path(os.environ.get('TORRENT_METADATA','/srv/qbittorrent/BT_backup'))

def decode(data):
    def item(i):
        if data[i:i+1] == b'i':
            e=data.index(b'e',i); return int(data[i+1:e]),e+1
        if data[i:i+1] in (b'l',b'd'):
            dictionary=data[i:i+1] == b'd'; i+=1; out={} if dictionary else []
            while data[i:i+1] != b'e':
                v,i=item(i)
                if dictionary:
                    w,i=item(i); out[v]=w
                else: out.append(v)
            return out,i+1
        e=data.index(b':',i); n=int(data[i:e]); return data[e+1:e+1+n],e+1+n
    return item(0)[0]

def torrent_paths(root):
    if root != DEFAULT: raise ValueError('Set MEDIA_ROOT to the exact managed root')
    if not BACKUP.is_dir(): raise RuntimeError('Torrent protection metadata missing; no files changed')
    protected=[]
    for f in BACKUP.glob('*.fastresume'):
        resume=decode(f.read_bytes()); torrent=decode(f.with_suffix('.torrent').read_bytes())
        saved=resume.get(b'qBt-savePath') or resume.get(b'save_path')
        name=torrent[b'info'].get(b'name.utf-8') or torrent[b'info'][b'name']
        if not saved or not name: raise RuntimeError('Unknown torrent path; no files changed')
        saved=saved.decode('utf-8'); name=name.decode('utf-8')
        for alias in ('/media', '/downloads'):
            if saved == alias or saved.startswith(alias+'/'):
                saved=str(root)+saved[len(alias):]
                break
        base=Path(saved)
        protected.append((base/name).resolve())
        for entry in torrent[b'info'].get(b'files',[]):
            parts=entry.get(b'path.utf-8') or entry[b'path']
            relative=Path(*(part.decode('utf-8') for part in parts))
            protected.append((base/relative).resolve())
            protected.append((base/name/relative).resolve())
        for mapped in resume.get(b'mapped_files',[]):
            if mapped: protected.append((base/mapped.decode('utf-8')).resolve())
    return protected

def clean(name):
    return re.sub(' +',' ',re.sub(r'[\[\]{}|<>-]',' ',name)).strip()

def arrange_audio(root,protected,dry_run=False):
    videos=[p for p in root.rglob('*') if p.is_file() and not p.is_symlink() and p.suffix.lower() in VIDEO_EXT]
    audio=[p for p in root.rglob('*') if p.is_file() and not p.is_symlink() and p.suffix.lower() in AUDIO_EXT]
    actions=[]; unmatched=[]; conflicts=[]
    def canon(s):return clean(s).casefold().strip()
    def episode(s):
        m=re.search(r's(\d{1,2})e(\d{1,3})',s,re.I)
        if m:return tuple(map(int,m.groups()))
        m=re.search(r'\b(\d{1,3})\s+(?:ma\d|(?:480|720|1080|2160)p)',s,re.I)
        return int(m.group(1)) if m else None
    for src in audio:
        stem=canon(src.stem);matches={};fallback=False
        for video in videos:
            base=canon(video.stem)
            if stem==base or stem.startswith(base+'.') or stem.startswith(base+' '):
                shared=len(Path(os.path.commonpath([src.parent,video.parent])).parts)
                matches[(video.parent,video.stem)]=len(src.parent.parts)+len(video.parent.parts)-2*shared
        if not matches and episode(stem) is not None:
            for video in videos:
                base=canon(video.stem)
                if video.parent==src.parent and episode(base)==episode(stem) and difflib.SequenceMatcher(None,stem,base).ratio()>0.9:
                    matches[(video.parent,video.stem)]=0;fallback=True
        if not matches:unmatched.append(str(src));continue
        nearest=min(matches.values());targets=[key for key,value in matches.items() if value==nearest]
        if len(targets)!=1:unmatched.append(str(src));continue
        parent,base=targets[0]
        if src.parent==parent and not fallback:continue
        parts=list(src.parent.relative_to(root).parts);marker=None;language=None
        for i,part in enumerate(parts):
            token=part.casefold().replace(' ','')
            if token.startswith('russound'):marker=i;language='rus'
            elif token.startswith('engsound'):marker=i;language='eng'
        labels=[clean(x).replace('.',' ').strip() for x in (parts[marker+1:] if marker is not None else [src.parent.name])]
        labels=[x for x in labels if x]
        if fallback:labels=['alternate',clean(src.stem.rsplit('.',1)[-1])]
        suffix='.'.join(([language] if language else [])+labels) or 'audio'
        dst=parent/(base+'.'+suffix+src.suffix.lower())
        if os.path.lexists(dst):
            if not dst.is_symlink() and os.path.samefile(src,dst):continue
            conflicts.append({'source':str(src),'target':str(dst)});continue
        protected_source=any(src==p or p in src.parents for p in protected)
        action={'operation':'link' if protected_source else 'move','source':str(src),'target':str(dst)}
        actions.append(action)
        if not dry_run:
            os.link(src,dst,follow_symlinks=False)
            if not protected_source:src.unlink()
    return {'actions':actions,'unmatched':unmatched,'conflicts':conflicts}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('root',nargs='?',default=str(DEFAULT)); ap.add_argument('--dry-run',action='store_true'); ap.add_argument('--apply',action='store_true')
    args=ap.parse_args(); args.dry_run=not args.apply or args.dry_run; Path(os.environ.get('ORGANIZER_STATE','state')).mkdir(parents=True,exist_ok=True); root=Path(args.root).resolve()
    if not root.is_dir() or root == Path('/'): raise RuntimeError('Invalid media root')
    if root == DEFAULT:
        None  # Optional external storage guard should run before this command.
    with open(str(Path(os.environ.get('ORGANIZER_STATE','state'))/'lock'),'a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: print('Another cleanup is running'); return
        protected=torrent_paths(root); changes=0; skips=0
        def guarded(p):
            p=p.resolve()
            return any(p == t or t in p.parents for t in protected)
        def move(src,dst):
            nonlocal changes,skips
            if src == dst or src.is_symlink() or guarded(src) or guarded(dst): skips+=1; return
            if os.path.lexists(dst): skips+=1; return
            if args.dry_run: print('Would rename:',src,'->',dst)
            else:
                # Hard-link then unlink is no-overwrite and preserves file data/metadata.
                os.link(src,dst,follow_symlinks=False)
                src.unlink()
            changes+=1
        for p,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if not Path(p,d).is_symlink() and not guarded(Path(p,d))]
            for name in files:
                src=Path(p,name); new=clean(name)
                if new and name != new: move(src,src.with_name(new))
        audio=arrange_audio(root,protected,args.dry_run)
        if not args.dry_run:
            for p,dirs,files in os.walk(root,topdown=False,followlinks=False):
                p=Path(p)
                if p!=root and not p.is_symlink() and not guarded(p) and any('sound' in x.casefold() for x in p.relative_to(root).parts):
                    try:p.rmdir()
                    except OSError:pass
            (Path(os.environ.get('ORGANIZER_STATE','state'))/'clean_media_last.json').write_text(json.dumps({'root':str(root),'renamed':changes,'audio':audio},ensure_ascii=False,indent=2))
        print('Dry run' if args.dry_run else 'Completed','changes=',changes,'skipped=',skips,'protected torrents=',len(protected),'audio arranged=',len(audio['actions']),'unmatched=',len(audio['unmatched']),'conflicts=',len(audio['conflicts']))

if __name__ == '__main__': main()

