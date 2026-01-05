import os

# Define the base folder relative to this script
PIPER_DIR = os.path.join(os.path.dirname(__file__), "piper")
RVC_DIR = os.path.join(os.path.dirname(__file__), "rvc_models")

# --- THE ULTIMATE CHARACTER DATABASE ---
CHARACTERS = {
    # =========================================================================
    # CHARACTER 1: AGNES TACHYON (The Mad Scientist)
    # =========================================================================
    "tachyon": {
        "name": "Agnes Tachyon",
        "piper_model": "en_GB-jenny_dioco-medium.onnx",
        "speaker_id": 0,
        
        # --- RVC SETTINGS (Disabled/Commented) ---
        # "rvc_enable": True,
        # "rvc_model": "Tachyon.pth",
        # "rvc_index": "Tachyon.index",
        # "pitch_shift": 0,
        
        "emotions": {
            # Note: "pitch_shift" is RVC-specific and currently ignored.
            
            # --- STANDARD CONVERSATION ---
            "default":   {"speed": 0.95, "pitch_shift": 0}, 
            "friendly":  {"speed": 1.00, "pitch_shift": 1},
            "curious":   {"speed": 1.00, "pitch_shift": 2}, 
            "concerned": {"speed": 0.90, "pitch_shift": -1}, 
            "explaining":{"speed": 0.95, "pitch_shift": 0}, 

            # --- HIGH ENERGY ---
            "manic":     {"speed": 1.15, "pitch_shift": 4}, 
            "eureka":    {"speed": 1.10, "pitch_shift": 3}, 
            "panic":     {"speed": 1.15, "pitch_shift": 2}, 
            "excited":   {"speed": 1.05, "pitch_shift": 1},
            "happy":     {"speed": 1.00, "pitch_shift": 1},
            "laugh":     {"speed": 1.05, "pitch_shift": 2},
            "surprised": {"speed": 1.10, "pitch_shift": 3}, 

            # --- LOW ENERGY ---
            "lazy":      {"speed": 0.80, "pitch_shift": -3}, 
            "tired":     {"speed": 0.75, "pitch_shift": -2},
            "bored":     {"speed": 0.80, "pitch_shift": -2},
            "sigh":      {"speed": 0.70, "pitch_shift": -4},
            "whisper":   {"speed": 0.85, "pitch_shift": -1},

            # --- SOCIAL / DARK ---
            "smug":      {"speed": 0.90, "pitch_shift": 0},   
            "mocking":   {"speed": 0.95, "pitch_shift": 1},   
            "serious":   {"speed": 0.85, "pitch_shift": -1},
            "cold":      {"speed": 0.90, "pitch_shift": -1},
            "sarcastic": {"speed": 0.85, "pitch_shift": -2}, 
            "flustered": {"speed": 1.05, "pitch_shift": 2},   
        }
    },

    # =========================================================================
    # CHARACTER 2: JEANNE ALTER (The Dragon Witch)
    # =========================================================================
    "jalter": {
        "name": "Jeanne Alter",
        "piper_model": "en_US-libritts-high.onnx",
        "speaker_id": 19, 
        
        # --- RVC SETTINGS (Disabled/Commented) ---
        # "rvc_enable": True,
        # "rvc_model": "jalter.pth", 
        # "rvc_index": "jalter.index",
        # "pitch_shift": -2, 
        
        "emotions": {
            # --- NEUTRAL / COOL ---
            "default":   {"speed": 0.90, "pitch_shift": -2}, 
            "friendly":  {"speed": 0.95, "pitch_shift": -1}, 
            "curious":   {"speed": 0.95, "pitch_shift": -1}, 
            "bored":     {"speed": 0.80, "pitch_shift": -2},
            "smug":      {"speed": 0.85, "pitch_shift": -1}, 

            # --- AGGRESSIVE ---
            "angry":     {"speed": 1.00, "pitch_shift": -1}, 
            "rage":      {"speed": 1.10, "pitch_shift": 1},  
            "command":   {"speed": 0.85, "pitch_shift": -4}, 
            "disgust":   {"speed": 0.75, "pitch_shift": -3}, 
            "annoyed":   {"speed": 0.90, "pitch_shift": -2}, 

            # --- TSUNDERE / VULNERABLE ---
            "tsundere":  {"speed": 1.05, "pitch_shift": 3},  
            "flustered": {"speed": 1.00, "pitch_shift": 1},  
            "shy":       {"speed": 0.95, "pitch_shift": 0},  
            "sad":       {"speed": 0.75, "pitch_shift": -1}, 
            "gentle":    {"speed": 0.90, "pitch_shift": -1}, 
            "surprised": {"speed": 1.05, "pitch_shift": 1},  
        }
    },

    # =========================================================================
    # CHARACTER 3: HATSUNE MIKU (The Virtual Idol)
    # =========================================================================
    "miku": {
        "name": "Hatsune Miku",
        "piper_model": "en_US-lessac-medium.onnx",
        "speaker_id": 0,
        
        # --- RVC SETTINGS (Disabled/Commented) ---
        # "rvc_enable": True,
        # "rvc_model": "miku.pth",      
        # "rvc_index": "miku.index",    
        # "pitch_shift": 4,             
        
        "emotions": {
            # --- DEFAULT STATE ---
            "default":   {"speed": 1.00, "pitch_shift": 4},
            "friendly":  {"speed": 1.05, "pitch_shift": 5}, 
            "curious":   {"speed": 1.05, "pitch_shift": 5}, 

            # --- HIGH ENERGY ---
            "happy":     {"speed": 1.05, "pitch_shift": 5},
            "excited":   {"speed": 1.10, "pitch_shift": 6}, 
            "sing":      {"speed": 0.95, "pitch_shift": 4}, 
            "shout":     {"speed": 1.15, "pitch_shift": 5}, 
            "cheer":     {"speed": 1.10, "pitch_shift": 6}, 
            "cute":      {"speed": 1.05, "pitch_shift": 6}, 

            # --- DIGITAL / NEGATIVE ---
            "system":    {"speed": 0.90, "pitch_shift": 2}, 
            "robot":     {"speed": 0.85, "pitch_shift": 0}, 
            "confused":  {"speed": 0.90, "pitch_shift": 3}, 
            "sad":       {"speed": 0.80, "pitch_shift": 2},
            "scared":    {"speed": 1.10, "pitch_shift": 5},
            "gentle":    {"speed": 0.90, "pitch_shift": 3}, 
        }
    }
}

def get_character_data(char_id):
    """Helper to retrieve full path and data safely"""
    char_id = char_id.lower()
    
    if char_id not in CHARACTERS:
        print(f"[DB ERROR] Character '{char_id}' not found. Defaulting to Tachyon.")
        char_id = "tachyon"
    
    data = CHARACTERS[char_id]
    model_path = os.path.join(PIPER_DIR, data["piper_model"])
    
    if not os.path.exists(model_path):
        print(f"[DB CRITICAL] Model missing at: {model_path}")
        print(f"Please download {data['piper_model']} and put it in the piper folder.")
        
    return data, model_path