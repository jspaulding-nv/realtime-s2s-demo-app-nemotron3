#!/bin/bash

# Real-Time Speech-to-Speech Translation Web Demo
# Starts both backend (FastAPI) and frontend (Vite) servers

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# PIDs for cleanup
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
    echo ""
    echo -e "${YELLOW}Shutting down servers...${NC}"

    if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        kill "$BACKEND_PID" 2>/dev/null
        echo -e "${GREEN}Backend stopped${NC}"
    fi

    if [ -n "$FRONTEND_PID" ] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
        kill "$FRONTEND_PID" 2>/dev/null
        echo -e "${GREEN}Frontend stopped${NC}"
    fi

    exit 0
}

trap cleanup SIGINT SIGTERM

echo "=============================================="
echo "  Real-Time Speech-to-Speech Translation"
echo "=============================================="
echo ""

# Check if backend dependencies are installed
echo -e "${YELLOW}Checking backend dependencies...${NC}"
if ! python3 -c "import fastapi, uvicorn" 2>/dev/null; then
    echo -e "${YELLOW}Installing backend dependencies...${NC}"
    pip install -r "$BACKEND_DIR/requirements.txt"
fi
echo -e "${GREEN}Backend dependencies OK${NC}"

# Check if frontend dependencies are installed
echo -e "${YELLOW}Checking frontend dependencies...${NC}"
if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    echo -e "${YELLOW}Installing frontend dependencies...${NC}"
    cd "$FRONTEND_DIR" && npm install
fi
echo -e "${GREEN}Frontend dependencies OK${NC}"

echo ""
echo -e "${GREEN}Starting servers...${NC}"
echo ""

# Start backend
cd "$BACKEND_DIR"
echo -e "${YELLOW}Starting backend on http://localhost:8000${NC}"
uvicorn main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# Wait a moment for backend to start
sleep 2

# Start frontend
cd "$FRONTEND_DIR"
echo -e "${YELLOW}Starting frontend on http://localhost:5173${NC}"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "=============================================="
echo -e "${GREEN}Servers running!${NC}"
echo ""
echo "  Frontend: http://localhost:5173"
echo "  Backend:  http://localhost:8000"
echo ""
echo "Press Ctrl+C to stop both servers"
echo "=============================================="
echo ""

# Wait for either process to exit
wait
