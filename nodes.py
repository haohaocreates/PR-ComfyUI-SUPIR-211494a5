import os
import torch
from torch.nn import functional as F
from contextlib import nullcontext
from omegaconf import OmegaConf

import comfy.model_management
import folder_paths
from nodes import ImageScaleBy
import torch.cuda
from .sgm.util import instantiate_from_config
script_directory = os.path.dirname(os.path.abspath(__file__))

class SUPIR_Upscale:
    def __init__(self):
        self.current_sdxl_model = None
    upscale_methods = ["nearest-exact", "bilinear", "area", "bicubic", "lanczos"]
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "supir_model": (folder_paths.get_filename_list("checkpoints"), ),
            "sdxl_model": (folder_paths.get_filename_list("checkpoints"), ),
            "image": ("IMAGE", ),
            "seed": ("INT", {"default": 123,"min": 0, "max": 0xffffffffffffffff, "step": 1}),
            "resize_method": (s.upscale_methods, {"default": "lanczos"}),
            "scale_by": ("FLOAT", {"default": 1.0, "min": 0.01, "max": 20.0, "step": 0.01}),
            "steps": ("INT", {"default": 45, "min": 3, "max": 4096, "step": 1}),
            "restoration_scale": ("FLOAT", {"default": -1.0, "min": -1.0, "max": 6.0, "step": 1.0}),
            "cfg_scale": ("FLOAT", {"default": 7.5,"min": 0, "max": 20, "step": 0.01}),
            "a_prompt": ("STRING", {"multiline": True, "default": "high quality, detailed",}),
            "n_prompt": ("STRING", {"multiline": True, "default": "bad quality, blurry, messy",}),
            "s_churn": ("INT", {"default": 5,"min": 0, "max": 40, "step": 1}),
            "s_noise": ("FLOAT", {"default": 1.003,"min": 1.0, "max": 1.1, "step": 0.001}),
            "control_scale": ("FLOAT", {"default": 1.0, "min": 0, "max": 1, "step": 0.05}),
            "cfg_scale_start": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 9.0, "step": 0.05}),
            "control_scale_start": ("FLOAT", {"default": 0.0, "min": 0, "max": 1.0, "step": 0.05}),
            "color_fix_type": (
            [   
                'None',
                'AdaIn',
                'Wavelet',
            ], {
               "default": 'AdaIn'
            }),
            "keep_model_loaded": ("BOOLEAN", {"default": True}),
            "use_tiled_vae": ("BOOLEAN", {"default": True}),
            "encoder_tile_size_pixels": ("INT", {"default": 512, "min": 64, "max": 8192, "step": 64}),
            "decoder_tile_size_latent": ("INT", {"default": 64, "min": 64, "max": 8192, "step": 64}),
            },
            "optional": {
                "captions": ("STRING", {"forceInput": True, "multiline": False, "default": "",}),
            }
            
            
            }
    
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES =("upscaled_image",)
    FUNCTION = "process"

    CATEGORY = "SUPIR"

    def process(self, steps, image, color_fix_type, seed, scale_by, cfg_scale, resize_method, s_churn, s_noise, encoder_tile_size_pixels, decoder_tile_size_latent,
                control_scale, cfg_scale_start, control_scale_start, restoration_scale, keep_model_loaded, 
                a_prompt, n_prompt, sdxl_model, supir_model, use_tiled_vae, captions=""):
        
        
        device = comfy.model_management.get_torch_device()
        image = image.to(device)
        
        SUPIR_MODEL_PATH = folder_paths.get_full_path("checkpoints", supir_model)
        SDXL_MODEL_PATH = folder_paths.get_full_path("checkpoints", sdxl_model)
        
        config_path = os.path.join(script_directory, "options/SUPIR_v0.yaml")

        if comfy.model_management.should_use_bf16():
            print("Using bf16")
            dtype = torch.bfloat16
            vae_dtype = 'bf16'
            model_dtype = 'bf16'
        elif comfy.model_management.should_use_fp16():
            print("Using fp16")
            dtype = torch.float16
            vae_dtype = 'fp32'
            model_dtype = 'fp16'
        else:
            print("Using fp32")
            dtype = torch.float32
            vae_dtype = 'fp32'
            model_dtype = 'fp32'

        if not hasattr(self, "model") or self.model is None or self.current_sdxl_model != sdxl_model:
            self.current_sdxl_model = sdxl_model
            config = OmegaConf.load(config_path)
            config.model.params.ae_dtype = vae_dtype
            config.model.params.diffusion_dtype = model_dtype
            self.model = instantiate_from_config(config.model).cpu()
            print(type(self.model))
            from .SUPIR.util import load_state_dict
            supir_state_dict = load_state_dict(SUPIR_MODEL_PATH)
            sdxl_state_dict = load_state_dict(SDXL_MODEL_PATH)
            self.model.load_state_dict(supir_state_dict, strict=False)
            self.model.load_state_dict(sdxl_state_dict, strict=False)
            self.model.to(device).to(dtype)
            
        if use_tiled_vae:
            self.model.init_tile_vae(encoder_tile_size=encoder_tile_size_pixels, decoder_tile_size=decoder_tile_size_latent, reset=False)
        else:
            self.model.init_tile_vae(encoder_tile_size=encoder_tile_size_pixels, decoder_tile_size=decoder_tile_size_latent, reset=True)
   
        autocast_condition = dtype == torch.float16 or torch.bfloat16 and not comfy.model_management.is_device_mps(device)
        with torch.autocast(comfy.model_management.get_autocast_device(device), dtype=dtype) if autocast_condition else nullcontext():
            
            image, = ImageScaleBy.upscale(self, image, resize_method, scale_by)
            B, H, W, C = image.shape
            new_height = H // 64 * 64
            new_width = W // 64 * 64
            image = image.permute(0, 3, 1, 2).contiguous()
            resized_image = F.interpolate(image, size=(new_height, new_width), mode='bicubic', align_corners=False)
                
            captions_list = []
            captions_list.append(captions)
            print(captions_list)
                
            use_linear_CFG = cfg_scale_start > 0
            use_linear_control_scale = control_scale_start > 0
            out = []
            pbar = comfy.utils.ProgressBar(B)
            for i in range(B):
                # # step 3: Diffusion Process
                samples = self.model.batchify_sample(resized_image[i].unsqueeze(0), captions_list, num_steps=steps, restoration_scale= restoration_scale, s_churn=s_churn,
                                                s_noise=s_noise, cfg_scale=cfg_scale, control_scale=control_scale, seed=seed,
                                                num_samples=1, p_p=a_prompt, n_p=n_prompt, color_fix_type=color_fix_type,
                                                use_linear_CFG=use_linear_CFG, use_linear_control_scale=use_linear_control_scale,
                                                cfg_scale_start=cfg_scale_start, control_scale_start=control_scale_start)
                
                out.append(samples.squeeze(0).cpu())
                print("Sampled image ", i, " out of ", B)
                pbar.update(1)
            if not keep_model_loaded:
                    self.model = None
            out_stacked = torch.stack(out, dim=0).cpu().to(torch.float32).permute(0, 2, 3, 1)
            return(out_stacked,)
    
NODE_CLASS_MAPPINGS = {
    "SUPIR_Upscale": SUPIR_Upscale,
    "SUPIR_Upscale": SUPIR_Upscale
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "SUPIR_Upscale": "SUPIR_Upscale",
    "SUPIR_Upscale": "SUPIR_Upscale"
}