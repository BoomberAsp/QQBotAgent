"""Publish a locally reviewed pack; no crawling or invented approval metadata."""
import argparse,sys,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'QQBot'))
from lib.sticker_library import publish
if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('manifest')
    parser.add_argument('--registry',default=os.environ.get('PERSONA_RESOURCE_REGISTRY',str(Path(__file__).resolve().parent.parent/'QQBot/data/persona_resources/registry.json')))
    args=parser.parse_args()
    print('Published',publish(args.manifest,args.registry),'stickers')
