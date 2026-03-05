"""
Robust DeepSeek API client with Firebase state management
Architectural Principles:
1. State Persistence: All operations synchronized to Firestore for crash recovery
2. Circuit Breaker: Automatic cooldown after consecutive failures
3. Exponential Backoff: Jittered retries for rate limits
4. Graceful Degradation: Fallback to alternative endpoints
"""

import logging
import time
import random
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
import json

# Core dependencies (no hallucinations)
import requests
from requests.exceptions import (
    RequestException, 
    Timeout, 
    ConnectionError, 
    HTTPError
)
import firebase_admin
from firebase_admin import credentials, firestore

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@dataclass
class APIState:
    """Immutable state container for API operations"""
    last_success: Optional[datetime] = None
    consecutive_failures: int = 0
    circuit_open: bool = False
    circuit_opened_at: Optional[datetime] = None
    total_requests: int = 0
    successful_requests: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to Firestore-compatible dictionary"""
        data = asdict(self)
        # Convert datetime to ISO string for Firestore
        for key, value in data.items():
            if isinstance(value, datetime):
                data[key] = value.isoformat()
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'APIState':
        """Reconstruct from Firestore dictionary"""
        # Convert ISO strings back to datetime
        processed = {}
        for key, value in data.items():
            if key in ['last_success', 'circuit_opened_at'] and value:
                processed[key] = datetime.fromisoformat(value)
            else:
                processed[key] = value
        return cls(**processed)

class RobustDeepSeekClient:
    """Production-grade DeepSeek API client with resilience patterns"""
    
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        firebase_credential_path: Optional[str] = None,
        project_id: str = "evolution-ecosystem",
        max_retries: int = 5,
        circuit_threshold: int = 3
    ):
        """
        Initialize client with robust error handling
        
        Args:
            api_key: DeepSeek API key
            base_url: API endpoint (configurable for testing)
            firebase_credential_path: Path to Firebase service account JSON
            project_id: Firebase project ID for state persistence
            max_retries: Maximum retry attempts with exponential backoff
            circuit_threshold: Failures before opening circuit
        """
        # Initialize all variables before use (CRITICAL)
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.max_retries = max_retries
        self.circuit_threshold = circuit_threshold
        self.session = None
        self.db = None
        self.state_doc_ref = None
        self._circuit_reset_seconds = 300  # 5 minutes cooldown
        
        # Validate required parameters
        if not api_key:
            raise ValueError("API key is required")
        
        # Initialize Firebase if credentials provided
        if firebase_credential_path:
            self._init_firebase(firebase_credential_path, project_id)
        
        # Initialize requests session with timeout
        self._init_session()
        
        # Load or create initial state
        self.state = self._load_state()
        
        logger.info(f"RobustDeepSeekClient initialized for {base_url}")
    
    def _init_firebase(self, credential_path: str, project_id: str):
        """Initialize Firebase connection with error handling"""
        try:
            cred = credentials.Certificate(credential_path)
            app = firebase_admin.initialize_app(cred, {
                'projectId