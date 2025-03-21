# full auto-regressive GeoGPT
# error accumulation uses a len 5 seq to finetune the model
import torch
import time
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl
from torch.optim.lr_scheduler import LambdaLR
from einops import rearrange

from src.main import instantiate_from_config

def disabled_train(self, mode=True):
    """Overwrite model.train with this function to make sure train/eval mode
    does not change anymore."""
    return self

class GeoTransformer(nn.Module):
    def __init__(self,
                 transformer_config,
                 first_stage_config,
                 cond_stage_config,
                 merge_channels=None,
                 use_depth=False,
                 ckpt_path=None,
                 ignore_keys=[],
                 first_stage_key="image",
                 cond_stage_key="depth",
                 use_scheduler=False,
                 scheduler_config=None,
                 emb_stage_config=None,
                 emb_stage_key="camera",
                 emb_stage_trainable=True,
                 top_k=None,
                 two_cond = False,
                 gradually = False,
                 ):

        super().__init__()
            
        self.init_first_stage_from_ckpt(first_stage_config)
        self.init_cond_stage_from_ckpt(cond_stage_config)
        self.transformer = instantiate_from_config(config=transformer_config)

        self.first_stage_key = first_stage_key
        self.cond_stage_key = cond_stage_key

        self.use_scheduler = use_scheduler
        
        if use_scheduler:
            assert scheduler_config is not None
            self.scheduler_config = scheduler_config
            
        self.emb_stage_key = emb_stage_key
        self.emb_stage_trainable = emb_stage_trainable and emb_stage_config is not None
        self.init_emb_stage_from_ckpt(emb_stage_config)
        self.top_k = top_k if top_k is not None else 100

        self.two_cond = two_cond
        self.gradually = gradually
        if gradually:
            print(f"yes")

        if ckpt_path is not None:
            self.init_from_ckpt(ckpt_path, ignore_keys=ignore_keys)
        
    def init_from_ckpt(self, path, ignore_keys=list()):
        sd = torch.load(path, map_location="cpu")
        for k in sd.keys():
            for ik in ignore_keys:
                if k.startswith(ik):
                    self.print("Deleting key {} from state_dict.".format(k))
                    del sd[k]
        missing, unexpected = self.load_state_dict(sd, strict=False)
        print(f"Restored from {path} with {len(missing)} missing keys and {len(unexpected)} unexpected keys.")

    def init_first_stage_from_ckpt(self, config):
        model = instantiate_from_config(config)
        self.first_stage_model = model.eval()
        self.first_stage_model.train = disabled_train

    def init_cond_stage_from_ckpt(self, config):
        if config == "__is_first_stage__":
            print("Using first stage also as cond stage.")
            self.cond_stage_model = self.first_stage_model
        else:
            model = instantiate_from_config(config)
            self.cond_stage_model = model.eval()
            self.cond_stage_model.train = disabled_train

    def init_emb_stage_from_ckpt(self, config):
        if config is None:
            self.emb_stage_model = None
        else:
            model = instantiate_from_config(config)
            self.emb_stage_model = model
            if not self.emb_stage_trainable:
                self.emb_stage_model.eval()
                self.emb_stage_model.train = disabled_train

    @torch.no_grad()
    def encode_to_z(self, x):
        quant_z, _, info = self.first_stage_model.encode(x)
        indices = info[2].view(quant_z.shape[0], -1)
        return quant_z, indices

    @torch.no_grad()
    def encode_to_c(self, c):
        quant_c, _, info = self.cond_stage_model.encode(c)
        indices = info[2].view(quant_c.shape[0], -1)
        return quant_c, indices

    def encode_to_e(self, batch):
        return self.emb_stage_model.process(batch)

    def get_normalized_c(self, batch):
        with torch.no_grad():
            quant_c, c_indices = self.encode_to_c(batch["src_img"])
            quant_d = None
       
        embeddings = self.encode_to_e(batch)
        dc_indices = c_indices

        # check that unmasking is correct
        total_cond_length = embeddings.shape[1] + dc_indices.shape[1]
        assert total_cond_length == self.transformer.config.n_unmasked, (
            embeddings.shape[1], dc_indices.shape[1], self.transformer.config.n_unmasked)

        return quant_d, quant_c, dc_indices, embeddings
    
    def encode_to_p(self, batch):
        inputs = []
        
        for k in ["R_rel", "t_rel", "K", "K_inv"]:
            entry = batch[k].reshape(batch[k].shape[0], -1)
            inputs.append(entry)
            
        p = torch.cat(inputs, dim=1) # B, 30

        return p
    
    def compute_camera_pose(self, R_dst, t_dst, R_src, t_src):
        R_src_inv = R_src.transpose(-1,-2)

        R_rel = torch.matmul(R_dst, R_src_inv)
        t_rel = t_dst.unsqueeze(-1)-torch.matmul(R_rel, t_src.unsqueeze(-1))

        return R_rel, t_rel[:, :, 0]

    def forward(self, batch, sample = False, top_k = 3, temperature = 0.1):
        #! 原本training 是拿3張GT圖片訓練,然後看2、3張有沒有預測正確
        #! error accumulate 就是說他拿3張GT 預測2、3張圖片
        #! 然後再拿預測的第2、3張圖片+第4張GT 預測第3、4張圖片...


        # get time
        B, time_len = batch["rgbs"].shape[0], batch["rgbs"].shape[2]        
        # set train pair
        gts = []
        forecasts = []
        
        # set seq
        video_clips = []
        video_clips.append(batch["rgbs"][:, :, 0, ...])
        video_clips.append(batch["rgbs"][:, :, 1, ...])
        
        # get gts
        gt_clips = []
        for t in range(1, time_len):
            _, c_indices = self.encode_to_c(batch["rgbs"][:, :, t, ...])            
            gt_clips.append(c_indices) # for loss

        # begin double
        for i in range(0, time_len-2):
            conditions = []
            p = []

            R_src = batch["R_s"][:, i, ...]
            t_src = batch["t_s"][:, i, ...]

            # create dict
            example = dict()
            example["K"] = batch["K"]
            example["K_inv"] = batch["K_inv"]
            
            # accumulate frame 0
            _, c_indices = self.encode_to_c(video_clips[-2])
            c_emb = self.transformer.tok_emb(c_indices)
            conditions.append(c_emb)

            # accumulate camera
            R_rel, t_rel = self.compute_camera_pose(batch["R_s"][:, i+1, ...], batch["t_s"][:, i+1, ...], R_src, t_src)
            example["R_rel"] = R_rel
            example["t_rel"] = t_rel
            embeddings_warp = self.encode_to_e(example)
            p.append(self.encode_to_p(example))
            conditions.append(embeddings_warp)

            # accumulate frame 1
            _, c_indices = self.encode_to_c(video_clips[-1])
            c_emb = self.transformer.tok_emb(c_indices)
            conditions.append(c_emb)

            # accumulate camera
            R_rel, t_rel = self.compute_camera_pose(batch["R_s"][:, i+2, ...], batch["t_s"][:, i+2, ...], R_src, t_src)
            example["R_rel"] = R_rel
            example["t_rel"] = t_rel
            embeddings_warp = self.encode_to_e(example)
            p.append(self.encode_to_p(example))
            conditions.append(embeddings_warp)
            
            # accumulate frame 2
            _, c_indices = self.encode_to_c(batch["rgbs"][:, :, i+2, ...])
            c_emb = self.transformer.tok_emb(c_indices)
            conditions.append(c_emb)
            
            # p3 
            R_rel, t_rel = self.compute_camera_pose(batch["R_s"][:, i+2, ...], batch["t_s"][:, i+2, ...], 
                                                    batch["R_s"][:, i+1, ...], batch["t_s"][:, i+1, ...])
            example["R_rel"] = R_rel
            example["t_rel"] = t_rel
            p.append(self.encode_to_p(example))

            # get logits
            conditions = torch.cat(conditions, 1) # B, L, 1024
            prototype = conditions[:, 0:286, :]
            z_emb = conditions[:, 286::, :]
            
            #* w2c 取其中3個
            logits, _ = self.transformer.iter_forward(prototype, z_emb, p = p,k=batch["K_ori"],w2c=batch['w2c_seq'][:,i:i+3,...])
            logits = logits[:, prototype.shape[1]-1:]
            
            for t in range(0, 2):
                # get prediction
                temp_logits = logits[:, 286*t:286*t+256, :]
                forecasts.append(temp_logits)
                predict = torch.argmax(temp_logits, 2)
                predict = self.decode_to_img(predict, [-1, 256, 16,16])
                video_clips.append(predict)
                # get gts
                gts.append(gt_clips[i+t])
            # print(f"forecasts len = {len(forecasts)}")
        
        # print(f"forecasts len = {len(forecasts)}")
        # print(f"forecasts shape = {forecasts[0].shape}")
        loss, log_dict = self.compute_loss(torch.cat(forecasts, 0), torch.cat(gts, 0), split="train")
        return forecasts, gts, loss, log_dict

    def cross_forward(self, batch,idx = 0):
        # get time
        B, time_len = batch["rgbs"].shape[0], batch["rgbs"].shape[2]        
        # set train pair
        gts = []
        forecasts = []
        
        # set seq
        video_clips = []
        video_clips.append(batch["rgbs"][:, :, 0, ...])
        
        # get gts
        gt_clips = []
        for t in range(1, time_len):
            _, c_indices = self.encode_to_c(batch["rgbs"][:, :, t, ...])            
            gt_clips.append(c_indices) # for loss

        #* 逐步的訓練, 讓模型能漸進學習
        if self.gradually:
            train_num = (idx//25000)+1
        else:
            train_num = time_len
        
        # print(f'idx = {idx}')
        # print(f"train num = {train_num}")
        
        for i in range(0,time_len-1):
            conditions = []
            p = []

            R_src = batch["R_s"][:, i, ...]
            t_src = batch["t_s"][:, i, ...]

            # create dict
            example = dict()
            example["K"] = batch["K"]
            example["K_inv"] = batch["K_inv"]

            # accumulate frame 0
            _, c_indices = self.encode_to_c(video_clips[-1])
            c_emb = self.transformer.tok_emb(c_indices)
            conditions.append(c_emb)

            # accumulate camera
            R_rel, t_rel = self.compute_camera_pose(batch["R_s"][:, i+1, ...], batch["t_s"][:, i+1, ...], R_src, t_src)
            example["R_rel"] = R_rel
            example["t_rel"] = t_rel
            embeddings_warp = self.encode_to_e(example)
            conditions.append(embeddings_warp)

            # get logits
            conditions = torch.cat(conditions, 1) # B, L, 1024
            prototype = conditions[:, 0:286, :]
            z_emb = conditions[:, 286::, :]

            #* ------------------------------------------------------------------------
            #* two condition 專用

            if self.two_cond==True: 
                two_cond_w2c = batch['w2c_seq'].clone()
                two_conditions = []
                if i == 0:
                    #* 因為一開始只有一張圖片，所以兩個condition 都選第一張
                    two_conditions.append(c_emb)
                    two_conditions.append(embeddings_warp)

                    two_cond_w2c[:,2] = batch['w2c_seq'][:,1].clone()
                    two_cond_w2c[:,1] = batch['w2c_seq'][:,0].clone()
                    two_cond_w2c[:,0] = batch['w2c_seq'][:,0].clone()
                else:
                    #*  前前張圖片的condition
                    _, c_indices = self.encode_to_c(video_clips[-2])
                    c_emb2 = self.transformer.tok_emb(c_indices)
                    two_conditions.append(c_emb2)

                    R_rel, t_rel = self.compute_camera_pose(batch["R_s"][:, i+1, ...], batch["t_s"][:, i+1, ...],
                                                             batch["R_s"][:, i-1, ...], batch["t_s"][:, i-1, ...])
                    example["R_rel"] = R_rel
                    example["t_rel"] = t_rel
                    embeddings_warp2 = self.encode_to_e(example)
                    two_conditions.append(embeddings_warp2)

                #* 前一張圖片的condition
                two_conditions.append(c_emb)
                two_conditions.append(embeddings_warp)

                two_conditions = torch.cat(two_conditions,1)
            #* ------------------------------------------------------------------------

            if self.two_cond==False:
                logits, _ = self.transformer.cross_forward(prototype,k=batch["K_ori"],w2c=batch['w2c_seq'][:,i:i+2,...])
            elif self.two_cond==True: 
                logits, _ = self.transformer.cross_forward(rgb1_emb = two_conditions[:,286:,:],
                                                           rgb0_emb = two_conditions[:,0:286,:],
                                                           k=batch["K_ori"],w2c= two_cond_w2c) 

            for t in range(0, 1):
                # get prediction
                temp_logits = logits[:, 286*t:286*t+256, :]
                forecasts.append(temp_logits)
                predict = torch.argmax(temp_logits, 2)
                predict = self.decode_to_img(predict, [-1, 256, 16,16])
                video_clips.append(predict)
                # get gts
                gts.append(gt_clips[i+t])
            
            if i+1 >= train_num:
                break

        loss, log_dict = self.compute_loss(torch.cat(forecasts, 0), torch.cat(gts, 0), split="train")
        
        last_fore = forecasts[-1]
        last_gt = gts[-1]
        while len(forecasts)<time_len-1:
            forecasts.append(last_fore)
            gts.append(last_gt)

        return forecasts, gts, loss, log_dict

    def top_k_logits(self, logits, k):
        v, ix = torch.topk(logits, k)
        out = logits.clone()
        out[out < v[..., [-1]]] = -float('Inf')
        return out

    @torch.no_grad()
    def sample_latent(self, x, c, steps, temperature=1.0, sample=False, top_k=None,
               callback=lambda k: None, embeddings=None, **kwargs):
        # in the current variant we always use embeddings for camera
        # assert embeddings is not None
        # check n_unmasked and conditioning length
        # total_cond_length = embeddings.shape[1] + c.shape[1]
        # assert total_cond_length == self.transformer.config.n_unmasked, (
        #     embeddings.shape[1], c.shape[1], self.transformer.config.n_unmasked)
        
        assert not self.transformer.training
        
        for k in range(steps):
            callback(k)
            x_cond = x            
            logits, _ = self.transformer.test(c, x_cond, embeddings=embeddings)
            logits = logits[:, -1, :] / temperature
            if top_k is not None:
                logits = self.top_k_logits(logits, top_k)
            probs = F.softmax(logits, dim=-1)
            
            if sample:
                ix = torch.multinomial(probs, num_samples=1)
            else:
                _, ix = torch.topk(probs, k=1, dim=-1)
                
            x = torch.cat((x, ix), dim=1)   

        return x
    
    @torch.no_grad()
    def sample(self, x, c, steps, temperature=1.0, sample=False, top_k=None,
               callback=lambda k: None, embeddings=None, **kwargs):
        # in the current variant we always use embeddings for camera
        assert embeddings is not None
        # check n_unmasked and conditioning length
        total_cond_length = embeddings.shape[1] + c.shape[1]
        assert total_cond_length == self.transformer.config.n_unmasked, (
            embeddings.shape[1], c.shape[1], self.transformer.config.n_unmasked)

        x = torch.cat((c,x),dim=1)
        block_size = self.transformer.get_block_size()
        assert not self.transformer.training
        for k in range(steps):
            callback(k)
            assert x.size(1) <= block_size  # make sure model can see conditioning
            # do not crop as this messes with n_unmasked
            #x_cond = x if x.size(1) <= block_size else x[:, -block_size:]  # crop context if needed
            x_cond = x
            logits, _ = self.transformer(x_cond, embeddings=embeddings)
            # pluck the logits at the final step and scale by temperature
            logits = logits[:, -1, :] / temperature
            # optionally crop probabilities to only the top k options
            if top_k is not None:
                logits = self.top_k_logits(logits, top_k)
            # apply softmax to convert to probabilities
            probs = F.softmax(logits, dim=-1)
            # sample from the distribution or take the most likely
            if sample:
                ix = torch.multinomial(probs, num_samples=1)
            else:
                _, ix = torch.topk(probs, k=1, dim=-1)
            # append to the sequence and continue
            x = torch.cat((x, ix), dim=1)
        # cut off conditioning
        x = x[:, c.shape[1]:]
        return x


    @torch.no_grad()
    def decode_to_img(self, index, zshape):
        bhwc = (zshape[0],zshape[2],zshape[3],zshape[1])
        quant_z = self.first_stage_model.quantize.get_codebook_entry(
            index.reshape(-1), shape=bhwc)
        x = self.first_stage_model.decode(quant_z)
        return x
    
    def compute_loss(self, logits, targets, split="train"):
        loss = F.cross_entropy(logits.reshape(-1, logits.size(-1)), targets.reshape(-1))
        return loss, {f"{split}/loss": loss.detach()}

    def configure_optimizers(self):
        # separate out all parameters to those that will and won't experience regularizing weight decay
        decay = set()
        no_decay = set()
        whitelist_weight_modules = (torch.nn.Linear, )
        blacklist_weight_modules = (torch.nn.LayerNorm, torch.nn.Embedding)
        for mn, m in self.transformer.named_modules():
            for pn, p in m.named_parameters():
                fpn = '%s.%s' % (mn, pn) if mn else pn # full param name

                if pn.endswith('bias'):
                    # all biases will not be decayed
                    no_decay.add(fpn)
                elif pn.endswith('weight') and isinstance(m, whitelist_weight_modules):
                    # weights of whitelist modules will be weight decayed
                    decay.add(fpn)
                elif pn.endswith('weight') and isinstance(m, blacklist_weight_modules):
                    # weights of blacklist modules will NOT be weight decayed
                    no_decay.add(fpn)

        # special case the position embedding parameter in the root GPT module as not decayed
        no_decay.add('frame_emb')
        no_decay.add('camera_emb')
        no_decay.add('time_emb')

        # validate that we considered every parameter
        param_dict = {pn: p for pn, p in self.transformer.named_parameters()}
        inter_params = decay & no_decay
        union_params = decay | no_decay
        assert len(inter_params) == 0, "parameters %s made it into both decay/no_decay sets!" % (str(inter_params), )
#         assert len(param_dict.keys() - union_params) == 0, "parameters %s were not separated into either decay/no_decay set!" \
#                                                     % (str(param_dict.keys() - union_params), )

        # create the pytorch optimizer object
        optim_groups = [
            {"params": [param_dict[pn] for pn in sorted(list(decay))], "weight_decay": 0.01},
            {"params": [param_dict[pn] for pn in sorted(list(param_dict.keys() - union_params))], "weight_decay": 0.0},
            {"params": [param_dict[pn] for pn in sorted(list(no_decay))], "weight_decay": 0.0},
        ]
        extra_parameters = list()
        if self.emb_stage_trainable:
            extra_parameters += list(self.emb_stage_model.parameters())
        
        optim_groups.append({"params": extra_parameters, "weight_decay": 0.0})
        print(f"Optimizing {len(extra_parameters)} extra parameters.")
        
        optimizer = torch.optim.AdamW(optim_groups, lr=self.learning_rate, betas=(0.9, 0.95))
        
        if self.use_scheduler:
            print("Setting up LambdaLR scheduler...")
            scheduler = instantiate_from_config(self.scheduler_config)
            scheduler = LambdaLR(optimizer, lr_lambda=scheduler.schedule)

            return optimizer, scheduler
        
        return optimizer