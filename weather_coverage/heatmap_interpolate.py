"""Nearest-neighbor and inverse-distance interpolation for weather layers."""
import numpy as np


def interpolate(model, values, method="idw", circular=False):
    indices=model["indices"]
    neighbors=values[np.maximum(indices,0)]
    mask=(indices>=0)&np.isfinite(neighbors)
    d=model["dist"]
    pos=np.argmax(mask,axis=1)
    nearest=neighbors[np.arange(len(pos)),pos]
    if method=="nearest":
        result=np.where(mask.any(axis=1),nearest,np.nan)
    else:
        weights=np.where(mask,1/np.maximum(d,1)**2,0)
        clean=np.where(mask,neighbors,0)
        if circular:
            radians=np.deg2rad(clean)
            y=(weights*np.sin(radians)).sum(axis=1)
            x=(weights*np.cos(radians)).sum(axis=1)
            result=np.mod(np.rad2deg(np.arctan2(y,x)),360)
            result=np.where(np.hypot(x,y)>weights.sum(axis=1)*1e-6,result,np.nan)
        else:
            result=(weights*clean).sum(axis=1)/np.maximum(weights.sum(axis=1),1e-30)
        result=np.where(mask.sum(axis=1)>=2,result,np.nan)
        result=np.where(mask[:,0]&(d[:,0]<=1),nearest,result)
    grid=np.full(model["ny"]*model["nx"],np.nan,dtype=np.float32)
    grid[model["pixels"]]=result
    return grid.reshape(model["ny"],model["nx"])
