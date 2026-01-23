#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import signal
import sys
from typing import Optional
import websockets
from websockets.exceptions import ConnectionClosed
import uuid
import hashlib
from aiohttp import web
import subprocess


RECONNECT_INTERVAL = 30
HEARTBEAT_INTERVAL = 60

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

class KubectlAgent:
    def __init__(self):
        self.ws_endpoint = os.getenv('NEXT_PUBLIC_WEBSOCKET_URL')
        self.agent_token = os.getenv('AURORA_AGENT_TOKEN')
        self.agent_version = os.getenv('AGENT_VERSION')
        
        # Derive cluster_id from token hash for consistency across restarts
        if self.agent_token:
            token_hash = hashlib.sha256(self.agent_token.encode()).hexdigest()
            self.cluster_id = f'cluster-{token_hash[:16]}'
        else:
            self.cluster_id = f'cluster-{uuid.uuid4().hex[:16]}'
        
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False
        self.shutdown = False
        self.health_server = None
        if not self.ws_endpoint:
            raise ValueError("NEXT_PUBLIC_WEBSOCKET_URL environment variable is required")
        if not self.agent_token:
            raise ValueError("AURORA_AGENT_TOKEN environment variable is required")
        logger.info(f"Initialized agent v{self.agent_version} for cluster: {self.cluster_id}")
    
    async def connect(self):
        try:
            self.websocket = await websockets.connect(
                self.ws_endpoint,
                additional_headers={
                    'Authorization': f'Bearer {self.agent_token}',
                    'X-Cluster-ID': self.cluster_id
                },
                ping_interval=20,
                ping_timeout=10
            )
            self.connected = True
            await self.send_message({
                'type': 'register',
                'cluster_id': self.cluster_id,
                'agent_version': self.agent_version
            })
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self.connected = False
            return False
    
    async def send_message(self, message: dict):
        if self.websocket and self.connected:
            try:
                await self.websocket.send(json.dumps(message))
            except Exception as e:
                logger.error(f"Failed to send message: {e}")
                self.connected = False
    
    async def heartbeat_loop(self):
        while not self.shutdown and self.connected:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self.connected:
                    await self.send_message({'type': 'heartbeat', 'cluster_id': self.cluster_id})
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
    
    async def execute_kubectl_command(self, command: str, timeout: int = 60) -> dict:
        """Execute kubectl command and return results."""
        try:
            result = subprocess.run(
                ['kubectl'] + command.split(),
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return {
                'success': result.returncode == 0,
                'output': result.stdout,
                'error': result.stderr if result.returncode != 0 else None,
                'return_code': result.returncode
            }
        except subprocess.TimeoutExpired:
            return {'success': False, 'error': f'Command timed out after {timeout} seconds', 'return_code': 124}
        except Exception as e:
            return {'success': False, 'error': str(e), 'return_code': 1}
    
    async def message_loop(self):
        try:
            async for message_text in self.websocket:
                try:
                    message = json.loads(message_text)
                    msg_type = message.get('type')
                    
                    if msg_type == 'connected':
                        logger.info(f"Connected to backend as {message.get('cluster_name')}")
                    elif msg_type == 'heartbeat_ack':
                        pass
                    elif msg_type == 'kubectl_command':
                        command_id = message.get('command_id')
                        command = message.get('command', '')
                        timeout = message.get('timeout', 60)
                        logger.info(f"Executing kubectl command: {command[:100]}")
                        result = await self.execute_kubectl_command(command, timeout)
                        await self.send_message({'type': 'command_response', 'command_id': command_id, **result})
                    elif msg_type:
                        logger.warning(f"Unknown message type: {msg_type}")
                        
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON received: {message_text}")
                except Exception as e:
                    logger.error(f"Error handling message: {e}")
        except ConnectionClosed:
            logger.warning("Connection closed by server")
            self.connected = False
        except Exception as e:
            logger.error(f"Message loop error: {e}")
            self.connected = False
    
    async def run(self):
        logger.info("Starting kubectl agent...")
        while not self.shutdown:
            try:
                if await self.connect():
                    heartbeat_task = asyncio.create_task(self.heartbeat_loop())
                    message_task = asyncio.create_task(self.message_loop())
                    done, pending = await asyncio.wait(
                        [heartbeat_task, message_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                if not self.shutdown:
                    logger.info(f"Reconnecting in {RECONNECT_INTERVAL} seconds...")
                    await asyncio.sleep(RECONNECT_INTERVAL)
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                if not self.shutdown:
                    await asyncio.sleep(RECONNECT_INTERVAL)
        logger.info("Agent shut down")
    
    def stop(self):
        logger.info("Shutting down agent...")
        self.shutdown = True
        self.connected = False
    
    async def start_health_server(self):
        app = web.Application()
        app.router.add_get('/health', self.health_handler)
        app.router.add_get('/ready', self.ready_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self.health_server = web.TCPSite(runner, '0.0.0.0', 8080)
        await self.health_server.start()
        logger.info("Health server started on port 8080")
    
    async def health_handler(self, request):
        return web.Response(text='ok', status=200)
    
    async def ready_handler(self, request):
        return web.Response(text='ready' if self.connected else 'not ready', status=200 if self.connected else 503)

async def main():
    agent = KubectlAgent()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, agent.stop)
    await asyncio.gather(agent.start_health_server(), agent.run())

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
