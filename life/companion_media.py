"""Real image attachments shared by local and remote companion chat."""
import base64
import hashlib
import io
from pathlib import Path

from PIL import Image, UnidentifiedImageError

MIMES = {'PNG':'image/png', 'JPEG':'image/jpeg', 'WEBP':'image/webp', 'GIF':'image/gif'}
MAX_BYTES = 8 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024


def _validate(raw, mime):
    if not raw or len(raw)>MAX_BYTES:
        raise ValueError('图片为空或超过8MB')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width*image.height>16_000_000:
                raise ValueError('图片分辨率超过1600万像素')
            actual=MIMES.get(image.format)
            if not actual or actual!=mime:
                raise ValueError('图片类型与实际文件不匹配')
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('无法读取该图片') from exc


def save_images(home, images=None):
    if images is None:return []
    if not isinstance(images,list) or len(images)>8:raise ValueError('一次最多发送8张图片')
    validated=[];total=0
    for item in images:
        if not isinstance(item,dict) or not isinstance(item.get('base64'),str) or len(item['base64'])>MAX_BYTES*4//3+4:
            raise ValueError('图片数据无效')
        try:raw=base64.b64decode(item['base64'],validate=True)
        except (ValueError,TypeError) as exc:raise ValueError('图片编码无效') from exc
        _validate(raw,item.get('mime'));total+=len(raw)
        if total>MAX_TOTAL:raise ValueError('一次发送的图片合计不能超过16MB')
        validated.append((item,raw))
    folder=Path(home)/'data/companion/images';folder.mkdir(parents=True,exist_ok=True)
    result=[]
    for item,raw in validated:
        handle=hashlib.sha256(raw).hexdigest();path=folder/handle
        if not path.exists():path.write_bytes(raw)
        result.append(dict(handle=handle,path=path.relative_to(home).as_posix(),mime=item['mime'],
                           name=Path(str(item.get('name') or 'image')).name[:120]))
    return result


def wire_images(home, images):
    result=[];root=Path(home).resolve()
    for image in images or []:
        path=(root/image['path']).resolve()
        if not path.is_relative_to(root/'data/companion/images'):raise ValueError('图片引用无效')
        if not path.is_file():continue
        raw=path.read_bytes();_validate(raw,image['mime'])
        result.append(dict(mime=image['mime'],name=image['name'],base64=base64.b64encode(raw).decode('ascii')))
    return result


def image_blocks(home, images):
    return [dict(type='image_url',image_url=dict(url='data:'+item['mime']+';base64,'+item['base64']))
            for item in wire_images(home,images)]


def outgoing_images(home, paths):
    if not isinstance(paths,list) or len(paths)>8:raise ValueError('一次最多发送8张图片')
    wire=[]
    for filename in paths:
        if not isinstance(filename,str):raise ValueError('图片必须是实际文件路径')
        path=Path(filename)
        if not path.is_absolute():path=Path(home)/path
        if not path.is_file() or path.stat().st_size>MAX_BYTES:raise ValueError('图片文件不存在或超过8MB')
        raw=path.read_bytes()
        try:
            with Image.open(io.BytesIO(raw)) as image:mime=MIMES.get(image.format)
        except (UnidentifiedImageError,OSError) as exc:raise ValueError('文件不是支持的图片') from exc
        wire.append(dict(mime=mime,name=path.name,base64=base64.b64encode(raw).decode('ascii')))
    return save_images(home,wire)
