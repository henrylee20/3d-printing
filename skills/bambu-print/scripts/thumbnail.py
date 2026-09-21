"""STL thumbnail rendering only; support decisions belong to Bambu Studio CLI."""
from pathlib import Path
import re,struct,zlib
import numpy as np


def triangles(path):
    data=Path(path).read_bytes()
    if len(data)>=84:
        count=struct.unpack_from('<I',data,80)[0]
        if count and 84+count*50==len(data):
            dtype=np.dtype([('normal','<f4',(3,)),('v','<f4',(3,3)),('attr','<u2')])
            return np.frombuffer(data,offset=84,count=count,dtype=dtype)['v'].astype(float)
    points=re.findall(rb'\bvertex\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)',data)
    if not points or len(points)%3:raise ValueError('不是有效的 STL 三角网格')
    return np.array(points,dtype=float).reshape(-1,3,3)


def preview_png(path,size=512):
    """Top-view mesh thumbnail; no GUI/display or extra rendering dependency."""
    tri=triangles(path);lo=tri.min(axis=(0,1));hi=tri.max(axis=(0,1));span=hi-lo
    scale=(size-28)/max(span[0],span[1],1e-6)
    xyz=tri.copy();xyz[:,:,:2]=(xyz[:,:,:2]-(lo[:2]+hi[:2])/2)*scale+size/2
    xyz[:,:,1]=size-xyz[:,:,1]
    rgb=np.full((size,size,3),246,dtype=np.uint8);depth=np.full((size,size),-np.inf)
    for original,t in zip(tri,xyz):
        xmin,ymin=np.maximum(0,np.floor(t[:,:2].min(axis=0)).astype(int));xmax,ymax=np.minimum(size-1,np.ceil(t[:,:2].max(axis=0)).astype(int))
        if xmin>xmax or ymin>ymax:continue
        (x0,y0,z0),(x1,y1,z1),(x2,y2,z2)=t
        det=(y1-y2)*(x0-x2)+(x2-x1)*(y0-y2)
        if abs(det)<1e-9:continue
        y,x=np.mgrid[ymin:ymax+1,xmin:xmax+1];x=x+.5;y=y+.5
        a=((y1-y2)*(x-x2)+(x2-x1)*(y-y2))/det
        b=((y2-y0)*(x-x2)+(x0-x2)*(y-y2))/det;c=1-a-b
        z=a*z0+b*z1+c*z2;target=depth[ymin:ymax+1,xmin:xmax+1]
        mask=(a>=-1e-6)&(b>=-1e-6)&(c>=-1e-6)&(z>=target)
        normal=np.cross(original[1]-original[0],original[2]-original[0]);normal/=max(np.linalg.norm(normal),1e-9)
        light=.65+.2*abs(float(normal[2]))+.15*float(normal[0]-normal[1])/2
        tint=np.clip(.8+.2*(z-lo[2])/max(span[2],1e-6),.7,1)
        color=np.clip(tint[:,:,None]*np.array([126,173,182])*light,0,255).astype(np.uint8)
        rgb[ymin:ymax+1,xmin:xmax+1][mask]=color[mask];target[mask]=z[mask]
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    raw=b''.join(b'\0'+row.tobytes() for row in rgb)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',size,size,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')
