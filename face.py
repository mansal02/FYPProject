import pygame
from pygame.locals import *
import os
import sys
import math
import time
import random

# --- LIVE2D IMPORT ---
try:
    from live2d import v3 as live2d
except ImportError:
    print("[CRITICAL] 'live2d' library not found.")
    sys.exit()

# --- CONFIGURATION ---
MODEL_PATH = r"d:\pylearn\FYP\AiAssistant\models\kei\runtime\kei_vowels_pro.model3.json"
WINDOW_WIDTH = 800
WINDOW_HEIGHT = 600

def map_range(value, in_min, in_max, out_min, out_max):
    return (value - in_min) * (out_max - out_min) / (in_max - in_min) + out_min

def main():
    # 1. SETUP
    pygame.init()
    pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT), DOUBLEBUF | OPENGL)
    pygame.display.set_caption("MARIE - Safe Mode")

    live2d.init()
    try:
        live2d.glInit()
    except:
        pass 

    if not os.path.exists(MODEL_PATH):
        print(f"[ERROR] Model not found: {MODEL_PATH}")
        return

    os.chdir(os.path.dirname(MODEL_PATH))
    model = live2d.LAppModel()
    model.LoadModelJson(MODEL_PATH)
    model.Resize(WINDOW_WIDTH, WINDOW_HEIGHT)

    # --- DEBUG: PRINT AVAILABLE PARAMETERS ---
    # This helps us see if "ParamAngleX" actually exists on your model
    print("\n[SYSTEM] Model Loaded. Available Parameters:")
    # Note: Some wrappers don't expose GetParameterCount directly, 
    # so we just skip the list if it fails to prevent startup crash.
    try:
        count = model.GetParameterCount()
        print(f"   > Found {count} controllable parts.")
    except:
        print("   > Could not list parameters (Safe Mode active)")

    print("\n[SYSTEM] Starting Loop. (Mouse Tracking + Safe Blinking Active)")

    # 2. VARIABLES
    clock = pygame.time.Clock()
    running = True
    
    t_breath = 0.0
    
    # BLINKING STATE (Managed by Python, not read from Model)
    blink_timer = time.time()
    next_blink_time = time.time() + 2.0
    is_blinking = False
    closing_eyes = True
    current_eye_openness = 1.0 # 1.0 = Open, 0.0 = Closed

    while running:
        # --- A. EVENTS ---
        for event in pygame.event.get():
            if event.type == QUIT:
                running = False
            elif event.type == KEYDOWN and event.key == K_ESCAPE:
                running = False

        # --- B. MOUSE TRACKING ---
        # Only run if mouse is inside window
        if pygame.mouse.get_focused():
            mx, my = pygame.mouse.get_pos()
            
            look_x = map_range(mx, 0, WINDOW_WIDTH, -30, 30)
            look_y = map_range(my, 0, WINDOW_HEIGHT, 30, -30)
            eye_x = map_range(mx, 0, WINDOW_WIDTH, -1.0, 1.0)
            eye_y = map_range(my, 0, WINDOW_HEIGHT, 1.0, -1.0)

            model.SetParameterValue("ParamAngleX", look_x)
            model.SetParameterValue("ParamAngleY", look_y)
            model.SetParameterValue("ParamEyeBallX", eye_x)
            model.SetParameterValue("ParamEyeBallY", eye_y)
            model.SetParameterValue("ParamBodyAngleX", look_x * 0.1)

        # --- C. SAFE BLINKING ---
        now = time.time()
        
        # 1. Decide when to blink
        if not is_blinking and now > next_blink_time:
            is_blinking = True
            closing_eyes = True
        
        # 2. Animate blink
        if is_blinking:
            if closing_eyes:
                current_eye_openness -= 0.2
                if current_eye_openness <= 0.0:
                    current_eye_openness = 0.0
                    closing_eyes = False
            else:
                current_eye_openness += 0.2
                if current_eye_openness >= 1.0:
                    current_eye_openness = 1.0
                    is_blinking = False
                    next_blink_time = now + random.uniform(2.0, 4.0)
            
            # 3. Apply to Model
            model.SetParameterValue("ParamEyeLOpen", current_eye_openness)
            model.SetParameterValue("ParamEyeROpen", current_eye_openness)

        # --- D. BREATHING ---
        t_breath += 0.05
        breath_val = (math.sin(t_breath) + 1) / 2
        model.SetParameterValue("ParamBreath", breath_val)

        # --- E. DRAW ---
        model.Update()
        
        live2d.clearBuffer(0.1, 0.1, 0.1, 1.0)
        model.Draw()
        pygame.display.flip()
        
        clock.tick(60)

    live2d.dispose()
    pygame.quit()
    sys.exit()

if __name__ == "__main__":
    main()