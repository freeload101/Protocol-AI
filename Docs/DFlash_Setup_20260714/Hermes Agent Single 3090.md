Here is the updated guide reflectling the switch to **Tiel-Coder-35B-A3B** (`Tiel-Coder-35B-A3B-UD-Q4_K_S.gguf`) along with the updated global defaults structure in your `models.ini`.

  

## Complete Setup & Optimization Guide

This guide covers the full setup pipeline for maximizing **Tiel-Coder-35B-A3B** performance on a single 24GB RTX 3090, including hardware-level tuning, inference server configuration, and Hermes Agent integration.

  

### 1. OS & Driver Preparation

To push the RTX 3090 to its limits, remove OS-level hardware locks and optimize how the NVIDIA driver manages VRAM.

  

#### Disable Windows Core Isolation

Windows Security blocks MSI Afterburner’s low-level ring-0 driver (`RTCore64.sys`) from modifying GPU power limit tables past 100%.

  

1. Open the Windows Start menu and search for **Core Isolation**.
    
      
    
2. Toggle **Memory Integrity** to **Off**.
    
      
    
3. Restart your computer.
    
      
    

#### NVIDIA Profile Inspector Tuning

Prevent the NVIDIA driver from offloading VRAM overflow to system RAM, which severely impacts token generation speeds.

  

1. Open `nvidiaProfileInspector.exe`.
    
      
    
2. Set or import the following profile (`global_profile_610.88_20261007_212938.nip`) to set **Power management mode** to Maximum Performance and disable **CUDA Sysmem Fallback Policy**:
    
      
    

XML

```
<?xml version="1.0" encoding="utf-16"?>
<ArrayOfProfile>
  <Profile>
    <Executeables />
    <Settings>
      <ProfileSetting>
        <SettingNameInfo>Power management mode</SettingNameInfo>
        <SettingID>274197361</SettingID>
        <SettingValue>1</SettingValue>
        <ValueType>Dword</ValueType>
      </ProfileSetting>
      <ProfileSetting>
        <SettingNameInfo>CUDA Sysmem Fallback Policy</SettingNameInfo>
        <SettingID>283962569</SettingID>
        <SettingValue>1</SettingValue>
        <ValueType>Dword</ValueType>
      </ProfileSetting>
      <ProfileSetting>
        <SettingNameInfo />
        <SettingID>1343646814</SettingID>
        <SettingValue>0</SettingValue>
        <ValueType>Dword</ValueType>
      </ProfileSetting>
    </Settings>
    <ExecutableFindFiles />
  </Profile>
</ArrayOfProfile>
```

### 2. RTX 3090 Hardware Tuning

LLM inference speed is primarily bound by GDDR6X memory bandwidth. Overclocking memory while undervolting the core yields the highest token-per-second output.

  

#### Step 1: Unlock MSI Afterburner

1. Open MSI Afterburner and click the **Gear icon (Settings)**.
    
      
    
2. Under the **General** tab, check:
    
      
    - **Unlock voltage control** (set to _Standard MSI_ or _Extended MSI_)
        
          
        
    - **Unlock voltage monitoring**
        
          
        
3. Click **Apply**, then **OK**, and restart Afterburner.
    
      
    

#### Step 2: Undervolt Core & Overclock Memory

- **Curve Editor (`Ctrl + F`):** Flatten your GPU core curve so it runs at **1850–1900 MHz** at **875mV–900mV**.
    
      
    
    > _Note:_ If temperatures remain cold (~52°C), you can push the flat line up to **1950 MHz at 925mV–950mV** to accelerate prompt evaluation. Alternative command line lock: `nvidia-smi -lgc 2100`.
    > 
    >   
    
- **VRAM Overclock:** Shift the **Mem (MHz)** slider from `+1000` up to **`+1100`** or **`+1200`**.
    
      
    
- **Test Memory Limits:** GDDR6X uses Error Detection and Retry (EDR). Pushing memory too far won't crash your system, but it will silently drop tokens/sec due to internal error correction. Test in 50 MHz increments and back off if speed drops.
    
      
    
- **Temperature Monitoring:** Monitor **GPU Memory Junction Temperature** using **HWiNFO64** (keep under 95°C; absolute maximum is 105°C).
    
      
    

### 3. Inference Engine Configuration (`models.ini`)

This updated configuration uses the **Tiel-Coder-35B-A3B-UD-Q4_K_S** model weights with a Shared Global Defaults block (`[*]`) and 8-bit KV caching (`ctk`/`ctv`) to cut memory overhead by 50% across a 200k context window.

  

Ini, TOML

```
[*]
# Shared Global Defaults

[SEC]
m = C:\backup\LLM_Server\models\models--peculiar-ragdoll--Cyber-Tiel-Coder-35B-A3B-GGUF-MTP\Cyber-Tiel-Coder-35B-A3B-MTP-UD-Q4_K_S.gguf
log-file = log.log
log-colors = off

; MTP speculation
spec-type = draft-mtp
spec-draft-n-max = 3
spec-draft-p-min = 0.0
spec-draft-n-ubatch = 512

; offload
ngl = all
ncmoe = 0
fa = on
sm = none

; batching
ubatch-size = 1024
batch-size = 2048

; template + context
jinja = true
no-reasoning-preserve = true
ctx-size = 131072
parallel = 1
ctk = q8_0
ctv = q8_0
ctkd = q8_0
ctvd = q8_0
no-context-shift = true

; sampling
temp = 0.6
top-k = 20
top-p = 0.95
min-p = 0
```

### 4. Hermes Agent Deployment & Local API Link

#### Installation & Setup

Bash

```
git clone https://github.com/NousResearch/hermes-agent.git
cd hermes-agent

pip install --upgrade pip setuptools wheel
pip install -e .

hermes update
```

#### Memory Tool Configuration

Bash

```
# Enable environment/project memory (MEMORY.md)
hermes config set memory.memory_enabled true

# Enable user preferences profile (USER.md)
hermes config set memory.user_profile_enabled true

# Enable built-in memory tools
hermes tools enable memory
```

#### Local Server API Key & Endpoint Link

To prevent HTTP 401 "Invalid API Key" errors when pointing Hermes to your local endpoint:

  

Bash

```
hermes config set model.provider custom
hermes config set model.base_url "http://localhost:8080/v1"
hermes config set model.api_key "sk-local"
hermes config set model.default "Tiel-Coder-35B-A3B"
```

_(If you use `hermes model`, select **Custom Endpoint**, point the URL to your `/v1` endpoint, enter `sk-local` as a placeholder key, and select your model.)_
