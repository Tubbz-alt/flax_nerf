import functools

import jax

from flax import linen as nn
from jax import numpy as jnp, lax, random


@functools.partial(jax.jit, static_argnums=(0, 1, 2))
def get_rays(img_h, img_w, focal, c2w):
    """Get ray origins and directions from a pinhole camera.
    Args:
        img_h: height in pixels
        img_w: width in pixels
        focal: focal length of the pinhole camera
        c2w: (3, 4) camera to world coordinate transformation matrix
    Returns:
        rays: (2, img_h * img_w, 3) stacked origin and direction rays
    """
    i, j = jnp.meshgrid(jnp.arange(img_w), jnp.arange(img_h), indexing="xy")
    dirs = jnp.stack(
        [
            (i - img_w * 0.5) / focal,
            -(j - img_h * 0.5) / focal,
            -jnp.ones_like(i),
        ],
        axis=-1,
    )
    rays_d = jnp.einsum("ijl,kl", dirs, c2w[:3, :3])
    rays_o = jnp.broadcast_to(c2w[:3, -1], rays_d.shape)
    return jnp.stack([rays_o, rays_d])


def ndc_rays(img_h, img_w, focal, near, rays_o, rays_d):
    """Normalized device coordinate rays.
    Space such that the canvas is a cube with sides [-1, 1] in each axis.
    Args:
        img_h: height in pixels
        img_w: width in pixels
        focal: focal length of the pinhole camera
        near: near depth bound for the scene
        rays_o: (num_rays, 3) origin rays
        rays_d: (num_rays, 3) direction rays
    Returns:
        rays_o: (num_rays, 3) origin rays in NDC
        rays_d: (num_rays, 3) direction rays in NDC
    """
    # shift ray origins to near plane
    t = -(near + rays_o[..., 2]) / rays_d[..., 2]
    rays_o = rays_o + t[..., None] * rays_d

    # projection
    o0 = -1.0 / (img_w / (2.0 * focal)) * rays_o[..., 0] / rays_o[..., 2]
    o1 = -1.0 / (img_h / (2.0 * focal)) * rays_o[..., 1] / rays_o[..., 2]
    o2 = 1.0 + 2.0 * near / rays_o[..., 2]

    d0 = (
        -1.0
        / (img_w / (2.0 * focal))
        * (rays_d[..., 0] / rays_d[..., 2] - rays_o[..., 0] / rays_o[..., 2])
    )
    d1 = (
        -1.0
        / (img_h / (2.0 * focal))
        * (rays_d[..., 1] / rays_d[..., 2] - rays_o[..., 1] / rays_o[..., 2])
    )
    d2 = -2.0 * near / rays_o[..., 2]

    rays_o = jnp.stack([o0, o1, o2], axis=-1)
    rays_d = jnp.stack([d0, d1, d2], axis=-1)

    return rays_o, rays_d


def prepare_rays(rays, hwf, config, near=0.0, far=1.0, c2w=None, c2w_static_cam=None):
    """
    Build rays for rendering.
    Args:
        rays: (2, num_rays, 3) origin and direction generated rays
        hwf: (3) tuple containing image height, width and focal length
        config: model and rendering config
        near: nearest distance for a ray
        far: farthest distance for a ray
        c2w: (3, 4) camera-to-world transformation matrix
        c2w_static_cam: (3, 4) transformation matrix for camera
    Returns:
        rays: (img_h, img_w, *) generated rays
    """
    if c2w is not None:
        # special case to render full image
        rays_o, rays_d = get_rays(*hwf, c2w)
    else:
        rays_o, rays_d = rays

    viewdirs = None
    if config.use_viewdirs:
        # provide ray directions as input
        viewdirs = rays_d
        if c2w_static_cam is not None:
            # special case to visualize effect of viewdirs
            rays_o, rays_d = get_rays(*hwf, c2w_static_cam)
        # make all directions unit magnitude
        viewdirs /= jnp.linalg.norm(viewdirs, axis=-1, keepdims=True)  # [num_rand, 3]

    # for forward facing scenes
    if config.llff.ndc and config.dataset_type == "llff":
        rays_o, rays_d = ndc_rays(*hwf, 1.0, rays_o, rays_d)

    near *= jnp.ones_like(rays_d[..., :1])
    far *= jnp.ones_like(rays_d[..., :1])

    if viewdirs is not None:
        rays = jnp.concatenate([rays_o, rays_d, near, far, viewdirs], axis=-1)
    else:
        rays = jnp.concatenate([rays_o, rays_d, near, far], axis=-1)
    return rays.astype(config.dtype)


def render_rays(rays, config, rng=None):
    """Render rays for the coarse model.
    Args:
        rays: (4, num_rays, *) generated rays
        config: model and rendering config
        rng: random key
    Returns:
        pts: (num_rays, num_samples, 3) points in space to evaluate model at
        z_vals: (num_rays, num_samples) depths of the sampled positions
    """
    rays_o, rays_d, near, far = rays

    # decide where to sample along each ray, all rays will be sampled at the same times
    t_vals = jnp.linspace(0.0, 1.0, config.num_samples)
    if config.llff.lindisp and config.dataset_type == "llff":
        # sample linearly in inverse depth (disparity)
        z_vals = 1.0 / (1.0 / near * (1.0 - t_vals) + 1.0 / far * t_vals)
    else:
        # space integration times linearly between 'near' and 'far'
        # same integration points will be used for all rays
        z_vals = near * (1.0 - t_vals) + far * t_vals
    z_vals = jnp.broadcast_to(z_vals, [rays_o.shape[0], config.num_samples])

    # perturbation sampling time along each ray
    if config.perturb and rng is not None:
        # get intervals between samples
        mids = 0.5 * (z_vals[..., 1:] + z_vals[..., :-1])
        upper = jnp.concatenate([mids, z_vals[..., -1:]], axis=-1)
        lower = jnp.concatenate([z_vals[..., :1], mids], axis=-1)
        # stratified samples in those intervals
        t_rand = random.uniform(rng, z_vals.shape)
        z_vals = lower + (upper - lower) * t_rand

    z_vals = z_vals.astype(rays_d.dtype)
    pts = rays_o[..., None, :] + rays_d[..., None, :] * z_vals[..., :, None]
    return pts, z_vals


def render_rays_fine(
    rays, z_vals, weights, num_importance, perturbation=True, rng=None
):
    """Render rays for the fine model.
    Args:
        rays: (2, num_rays, 3) origin and direction generated rays
        z_vals: (num_rays, num_samples) depths of the sampled positions
        weights: (num_rays, num_samples) weights assigned to each sampled color for the coarse model
        num_importance: number of samples used in the fine model
        perturbation: whether to apply jitter on each ray or not
        rng: random key
    Returns:
        pts: (num_rays, num_samples + num_importance, 3) points in space to evaluate model at
        z_vals: (num_rays, num_samples + num_importance) depths of the sampled positions
        z_samples: (num_rays) standard deviation of distances along ray for each sample
    """
    rays_o, rays_d = rays

    z_vals_mid = 0.5 * (z_vals[..., 1:] + z_vals[..., :-1])
    z_samples = sample_pdf(
        z_vals_mid, weights[..., 1:-1], num_importance, perturbation, rng
    )
    z_samples = lax.stop_gradient(z_samples)

    # obtain all points to evaluate color density at
    z_vals = jnp.sort(jnp.concatenate([z_vals, z_samples], axis=-1), axis=-1)
    z_vals = z_vals.astype(rays_d.dtype)
    pts = rays_o[..., None, :] + rays_d[..., None, :] * z_vals[..., :, None]
    return pts, z_vals, jnp.std(z_samples, axis=-1)


def sample_pdf(bins, weights, num_importance, perturbation, rng):
    """Hierarchical sampler.
    Sample `num_importance` rays from `bins` with distribution defined by `weights`.
    Args:
        bins: (num_rays, num_samples - 1) bins to sample from
        weights: (num_rays, num_samples - 2) weights assigned to each sampled color for the coarse model
        num_importance: the number of samples to draw from the distribution
        perturbation: whether to apply jitter on each ray or not
        rng: random key
    Returns:
        samples: (num_rays, num_importance) the sampled rays
    """
    # get pdf
    weights = jnp.clip(weights, 1e-5)  # prevent NaNs
    pdf = weights / jnp.sum(weights, axis=-1, keepdims=True)
    cdf = jnp.cumsum(pdf, axis=-1)
    cdf = jnp.concatenate([jnp.zeros_like(cdf[..., :1]), cdf], axis=-1)

    # take uniform samples
    samples_shape = [*cdf.shape[:-1], num_importance]
    if perturbation:
        uni_samples = random.uniform(rng, shape=samples_shape)
    else:
        uni_samples = jnp.linspace(0.0, 1.0, num_importance)
        uni_samples = jnp.broadcast_to(uni_samples, samples_shape)

    # invert CDF
    idx = jax.vmap(lambda x, y: jnp.searchsorted(x, y, side="right"))(cdf, uni_samples)

    below = jnp.maximum(0, idx - 1)
    above = jnp.minimum(cdf.shape[-1] - 1, idx)
    inds_g = jnp.stack([below, above], axis=-1)

    cdf_g = jnp.take_along_axis(cdf[..., None], inds_g, axis=1)
    bins_g = jnp.take_along_axis(bins[..., None], inds_g, axis=1)

    denom = cdf_g[..., 1] - cdf_g[..., 0]
    # denom = jnp.where(denom < 1e-5, jnp.ones_like(denom), denom)
    denom = lax.select(denom < 1e-5, jnp.ones_like(denom), denom)
    t = (uni_samples - cdf_g[..., 0]) / denom
    samples = bins_g[..., 0] + t * (bins_g[..., 1] - bins_g[..., 0])
    return samples


def raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd, rng=None):
    """Transforms model's predictions to semantically meaningful values.
    Args:
        raw: (num_rays, num_samples || num_importance, 4) prediction from model
        z_vals: (num_rays, num_samples || num_importance) integration time
        rays_d: (num_rays, 3) direction of each ray
        raw_noise_std: std of noise added for regularization
        white_bkgd: whether to use the alpha channel for white background
        rng: random key
    Returns:
        acc_map: (num_rays) sum of weights along each ray
        depth_map: (num_rays) estimated distance to object
        disp_map: (num_rays) disparity map (inverse of depth map)
        rgb_map: (num_rays, 3) estimated RGB color of a ray
        weights: (num_rays, num_samples || num_importance) weights assigned to each sampled color
    """

    # compute 'distance' (in time) between each integration time along a ray
    dists = z_vals[..., 1:] - z_vals[..., :-1]

    # the 'distance' from the last integration time is infinity
    dists = jnp.concatenate(
        [dists, jnp.broadcast_to([1e10], dists[..., :1].shape)], axis=-1
    )
    dists = dists.astype(z_vals.dtype)  # [num_rays, num_samples]

    # multiply each distance by the norm of its corresponding direction ray
    # to convert to real world distance (accounts for non-unit directions)
    dists = dists * jnp.linalg.norm(rays_d[..., None, :], axis=-1)

    # extract RGB of each sample position along each ray
    rgb = nn.sigmoid(raw[..., :3])  # [num_rays, num_samples, 3]

    # add noise to predictions for density, can be used to (this value is strictly between [0, 1])
    # regularize network during training (prevents floater artifacts)
    noise = 0.0
    if raw_noise_std > 0.0 and rng is not None:
        noise = random.normal(rng, raw[..., 3].shape) * raw_noise_std

    # predict density of each sample along each ray (alpha channel)
    # higher values imply higher likelihood of being absorbed at this point
    alpha = 1.0 - jnp.exp(-nn.relu(raw[..., 3] + noise) * dists)

    # compute weight for RGB of each sample along each ray
    # cumprod() is used to express the idea of the ray not having reflected up to this sample yet
    # weights = alpha * tf.math.cumprod(1.0 - alpha + 1e-10, axis=-1, exclusive=True)
    alpha_ = jnp.clip(1.0 - alpha, 1e-5, 1.0)
    weights = jnp.concatenate([jnp.ones_like(alpha_[..., :1]), alpha_[..., :-1]], -1)
    weights = alpha * jnp.cumprod(weights, -1)  # [num_rays, num_samples]

    # computed weighted color of each sample along each ray
    rgb_map = jnp.einsum("ij,ijk->ik", weights, rgb)  # [num_rays, 3]

    # estimated depth map is expected distance
    depth_map = jnp.einsum("ij,ij->i", weights, z_vals)  # [num_rays]

    # sum of weights along each ray (this value is in [0, 1] up to numerical error)
    acc_map = jnp.einsum("ij->i", weights)  # [num_rays]

    # disparity map is inverse depth
    i_depth = depth_map / jnp.clip(acc_map, 1e-5)
    disp_map = 1.0 / jnp.clip(i_depth, 1e-5)

    # to composite onto a white background, use the accumulated alpha map
    if white_bkgd:
        rgb_map += 1.0 - acc_map[..., None]

    return {
        "rgb": rgb_map.astype(jnp.float32),
        "disp": disp_map.astype(jnp.float32),
        "acc": acc_map.astype(jnp.float32),
        "depth": depth_map.astype(jnp.float32),
    }, weights
