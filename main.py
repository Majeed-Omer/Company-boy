from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from database import get_user, create_user, verify_password, get_db_connection
from database import get_all_policies
from database import save_chat
from database import get_chat_history


cached_policies_text = None

import os
from dotenv import load_dotenv
import uvicorn
import requests


load_dotenv()

app = FastAPI()

OLLAMA_API_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "company-bot"

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY"),
    session_cookie="secure_session",
    https_only=False,  # Set to True in production
    same_site="lax"
)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Routes
@app.get("/")
async def home(request: Request):
    if not request.session.get("user"):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("index.html", {"request": request, "username": request.session.get("user")})

@app.get("/login")
async def login_page(request: Request):
    # If already logged in, redirect to home
    if request.session.get("user"):
        return RedirectResponse(url="/")
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    # If already logged in, redirect to home
    if request.session.get("user"):
        return RedirectResponse(url="/")
    
    user = await get_user(username)
    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Invalid credentials"
        }, status_code=401)
    
    # Update last login
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE username = %s",
        (username,)
    )
    conn.commit()
    cursor.close()
    conn.close()
    
    request.session["user"] = username
    return RedirectResponse(url="/", status_code=302)  # Redirect to home page

@app.get("/signup")
async def signup_page(request: Request):
    # If already logged in, redirect to home
    if request.session.get("user"):
        return RedirectResponse(url="/")
    return templates.TemplateResponse("signup.html", {"request": request})

@app.post("/signup")
async def signup(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...)
):
    # If already logged in, redirect to home
    if request.session.get("user"):
        return RedirectResponse(url="/")
    
    if password != confirm_password:
        return templates.TemplateResponse("signup.html", {
            "request": request,
            "error": "Passwords don't match"
        }, status_code=400)
    
    if await get_user(username):
        return templates.TemplateResponse("signup.html", {
            "request": request,
            "error": "Username already exists"
        }, status_code=400)
    
    if await create_user(username, password):
        # Auto-login after successful registration
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=302)  # Redirect to home page
    else:
        return templates.TemplateResponse("signup.html", {
            "request": request,
            "error": "Registration failed"
        }, status_code=500)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")

@app.post("/chat")
async def chat(request: Request):
    try:
        data = await request.json()
        user_message = data.get("message")
        if not user_message:
            raise HTTPException(status_code=400, detail="Message is required")

        policies_text = get_cached_policies()  # ✅ Load once

        system_prompt = (
            "You are ACME Telecom's virtual assistant. Answer strictly based on the following monitoring policies:\n\n"
            + policies_text
            + "\n\nOnly respond with the approved policy information."
        )

        payload = {
            "model": MODEL_NAME,
            "prompt": f"{system_prompt}\n\nUser: {user_message}\n\nAssistant:",
            "stream": False
        }

        # ⏱ Add timeout
        response = requests.post(OLLAMA_API_URL, json=payload, timeout=1000)
        response.raise_for_status()
        data = response.json()

        reply = data.get("message", {}).get("content", "") or data.get("response", "") or data.get("text", "")
        if not reply:
            return JSONResponse(content={"response": "Sorry, I didn't get a valid response from the model."}, status_code=200)

        username = request.session.get("user")
        if username:
            save_chat(username, user_message, reply)

        return {"response": reply}

    except requests.exceptions.Timeout:
        return JSONResponse(content={"response": "The bot took too long to respond. Please try again."}, status_code=504)
    except requests.exceptions.RequestException as e:
        print("Ollama Error:", str(e))
        raise HTTPException(status_code=502, detail="Failed to communicate with the Ollama API")
    except Exception as e:
        print("Chat Error:", str(e))
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/history", response_class=HTMLResponse)
async def chat_history(request: Request):
    username = request.session.get("user")
    if not username:
        return RedirectResponse(url="/login")

    history = get_chat_history(username)
    return templates.TemplateResponse("history.html", {
        "request": request,
        "history": history
    })

def get_cached_policies():
    global cached_policies_text
    if cached_policies_text is None:
        policies = get_all_policies()
        # cached_policies_text = "\n\n---\n\n".join([p["content"] for p in policies])
        cached_policies_text = "\n\n---\n\n".join([
    p["content"][:500] + "..." if len(p["content"]) > 500 else p["content"]
    for p in policies
])

    return cached_policies_text


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
