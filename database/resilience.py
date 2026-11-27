from sqlalchemy import exc, event
from sqlalchemy.engine import Engine
import time
import logging

logger = logging.getLogger(__name__)

def setup_retry_events(engine: Engine, max_retries: int = 3, retry_delay: float = 0.5):
    """
    Configura eventos no SQLAlchemy para lidar com desconexões e OperationalError.
    
    Baseado em: https://docs.sqlalchemy.org/en/20/core/events.html
    """

    @event.listens_for(engine, "handle_error")
    def handle_exception(exception_context):
        """
        Intercepta OperationalError (MySQL server has gone away).
        Se for o erro esperado, tenta invalidar a conexão para que o pool crie uma nova.
        """
        if isinstance(exception_context.original_exception, exc.OperationalError):
            # Erros comuns de desconexão no MySQL/PyMySQL:
            # 2006: MySQL server has gone away
            # 2013: Lost connection to MySQL server during query
            error_code = getattr(exception_context.original_exception.orig, 'args', [None])[0]
            
            if error_code in (2006, 2013):
                logger.warning(f"MySQL connection lost (error {error_code}). Invaliding pool.")
                exception_context.is_disconnect = True
        
        return None # Return None to let SQLAlchemy handle the exception normally

import functools

def db_retry(max_retries=3, delay=1):
    """
    Decorador para retentar queries/operações de banco de dados.
    Útil para workers e tarefas de background.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (exc.OperationalError, exc.StatementError) as e:
                    last_error = e
                    # Verifica se é erro de conexão
                    is_disconnect = False
                    if isinstance(e, exc.OperationalError):
                        error_code = getattr(e.orig, 'args', [None])[0]
                        if error_code in (2006, 2013):
                            is_disconnect = True
                    
                    if is_disconnect or "server has gone away" in str(e).lower():
                        logger.warning(f"Retry {attempt + 1}/{max_retries} due to DB disconnect: {e}")
                        time.sleep(delay * (attempt + 1)) # Exponential backoff simples
                        continue
                    raise # Se não for erro de conexão, lança imediatamente
            raise last_error
        return wrapper
    return decorator
