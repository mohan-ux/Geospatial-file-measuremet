# Production Deployment Guide

This guide covers best practices for deploying the Geospatial File Measurement API to production.

## Table of Contents
- [Pre-Deployment Checklist](#pre-deployment-checklist)
- [Environment Configuration](#environment-configuration)
- [Security Hardening](#security-hardening)
- [Performance Optimization](#performance-optimization)
- [Monitoring & Logging](#monitoring--logging)
- [Scaling Considerations](#scaling-considerations)
- [Backup & Recovery](#backup--recovery)

---

## Pre-Deployment Checklist

- [ ] All tests passing (`pytest -v`)
- [ ] Environment variables configured properly
- [ ] Database backups scheduled
- [ ] Logging configured for production
- [ ] CORS origins restricted to specific domains
- [ ] Rate limiting implemented
- [ ] SSL/TLS certificates configured
- [ ] Monitoring and alerting set up
- [ ] Documentation up to date

---

## Environment Configuration

### Required Environment Variables

Create a `.env` file with production values:

```env
# Application
APP_NAME="Geospatial File Measurement API"
APP_VERSION="1.0.0"
DEBUG=false                          # MUST be false in production

# Database - Use PostgreSQL in production
DATABASE_URL="postgresql+asyncpg://user:password@host:5432/dbname"

# File Storage
UPLOAD_DIR="/var/app/uploads"        # Absolute path recommended
MAX_UPLOAD_SIZE_MB=100               # Adjust based on your needs
DELETE_FILE_ON_FAILURE=true          # Clean up failed uploads

# Security (optional additions)
SECRET_KEY="your-secret-key-here"    # For JWT if you add authentication
ALLOWED_HOSTS="api.yourdomain.com,www.yourdomain.com"
```

### Database Migration to PostgreSQL

For production, PostgreSQL with PostGIS is recommended:

```bash
# Install PostGIS extension
CREATE EXTENSION postgis;

# Update DATABASE_URL
DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/geo_api"
```

---

## Security Hardening

### 1. CORS Configuration

**Current (Development):**
```python
allow_origins=["*"]  # Accepts all origins
```

**Production (app/main.py):**
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://yourdomain.com",
        "https://www.yourdomain.com",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)
```

### 2. Rate Limiting

Install and configure slowapi:

```bash
pip install slowapi
```

```python
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.post("/api/files/")
@limiter.limit("10/minute")  # 10 uploads per minute per IP
async def upload_file(...):
    ...
```

### 3. Authentication & Authorization

For production, implement JWT-based authentication:

```bash
pip install python-jose[cryptography] passlib[bcrypt]
```

Add authentication middleware and protect endpoints with:
```python
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer()

async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    # Verify JWT token
    ...
```

### 4. Input Validation

Current implementation already includes:
- ✅ File type validation (extension check)
- ✅ File size limits with streaming validation
- ✅ Zip bomb protection (uncompressed size check)
- ✅ Path traversal prevention
- ✅ Filename sanitization
- ✅ SQL injection protection (using ORM)

### 5. Security Headers

Already implemented:
- ✅ X-Content-Type-Options: nosniff
- ✅ X-Frame-Options: DENY
- ✅ X-XSS-Protection: 1; mode=block
- ✅ Strict-Transport-Security (HSTS)

---

## Performance Optimization

### 1. Async Background Processing

For production workloads, move file processing to background tasks:

```bash
pip install celery redis
```

**app/tasks.py:**
```python
from celery import Celery

celery_app = Celery('geospatial_api', broker='redis://localhost:6379/0')

@celery_app.task
def process_file_task(file_record_id: str):
    # Move process_file logic here
    ...
```

**app/api/routes/files.py:**
```python
@router.post("/api/files/")
async def upload_file(...):
    # Save file and create record with status=PENDING
    ...
    # Enqueue background task
    process_file_task.delay(record.id)
    return record  # Return immediately
```

### 2. Database Connection Pooling

Configure async connection pool:

```python
engine = create_async_engine(
    settings.database_url,
    echo=False,  # Disable SQL logging in production
    pool_size=20,  # Number of connections to maintain
    max_overflow=10,  # Additional connections when pool is full
    pool_pre_ping=True,  # Test connections before use
    pool_recycle=3600,  # Recycle connections after 1 hour
)
```

### 3. File Storage

For production, use object storage (S3, GCS, Azure Blob):

```bash
pip install aioboto3  # For AWS S3
```

```python
import aioboto3

async def upload_to_s3(file_path: str, bucket: str, key: str):
    session = aioboto3.Session()
    async with session.client('s3') as s3:
        await s3.upload_file(file_path, bucket, key)
```

### 4. Caching

Add Redis caching for frequently accessed data:

```bash
pip install aioredis
```

```python
import aioredis

redis = await aioredis.create_redis_pool('redis://localhost')

# Cache file metadata
await redis.setex(f"file:{file_id}", 3600, json.dumps(file_data))
```

### 5. CDN Integration

Serve static content and API responses through a CDN (CloudFlare, CloudFront).

---

## Monitoring & Logging

### 1. Structured Logging

Replace basic logging with structured logging:

```bash
pip install structlog
```

```python
import structlog

logger = structlog.get_logger()
logger.info("file_uploaded", file_id=file_id, size_mb=size_mb, user_id=user_id)
```

### 2. Application Monitoring

Integrate with monitoring services:

**Sentry (Error Tracking):**
```bash
pip install sentry-sdk[fastapi]
```

```python
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

sentry_sdk.init(
    dsn="your-sentry-dsn",
    integrations=[FastApiIntegration()],
    traces_sample_rate=0.1,  # 10% of transactions
)
```

**Prometheus (Metrics):**
```bash
pip install prometheus-fastapi-instrumentator
```

```python
from prometheus_fastapi_instrumentator import Instrumentator

instrumentator = Instrumentator()
instrumentator.instrument(app).expose(app)
```

### 3. Health Check Enhancements

Add database connectivity check:

```python
@app.get("/health")
async def health(db: AsyncSession = Depends(get_session)):
    try:
        await db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    
    return {
        "status": "ok",
        "database": db_status,
        "version": settings.app_version,
    }
```

### 4. Log Aggregation

Use centralized logging:
- **ELK Stack** (Elasticsearch, Logstash, Kibana)
- **Datadog**
- **CloudWatch** (AWS)

---

## Scaling Considerations

### Horizontal Scaling

The application is stateless and can scale horizontally:

1. **Load Balancer**: Nginx, HAProxy, or cloud load balancer
2. **Multiple Workers**: Run multiple uvicorn processes
3. **Auto-Scaling**: Configure based on CPU/memory metrics

**Nginx Configuration:**
```nginx
upstream api_backend {
    server 127.0.0.1:8001;
    server 127.0.0.1:8002;
    server 127.0.0.1:8003;
}

server {
    listen 80;
    server_name api.yourdomain.com;

    location / {
        proxy_pass http://api_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        
        # Increase timeouts for large file uploads
        client_max_body_size 100M;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }
}
```

### Vertical Scaling

- **CPU**: Multi-core processors for GeoPandas/Shapely operations
- **RAM**: Minimum 4GB, recommended 8GB+ for large files
- **Disk**: SSD for faster file I/O

### Container Deployment

**Dockerfile:**
```dockerfile
FROM python:3.11-slim

# Install GDAL dependencies
RUN apt-get update && apt-get install -y \
    gdal-bin \
    libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

**docker-compose.yml:**
```yaml
version: '3.8'

services:
  api:
    build: .
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql+asyncpg://user:password@db:5432/geo_api
      - DEBUG=false
    volumes:
      - ./uploads:/app/uploads
    depends_on:
      - db
      - redis

  db:
    image: postgis/postgis:15-3.3
    environment:
      POSTGRES_USER: user
      POSTGRES_PASSWORD: password
      POSTGRES_DB: geo_api
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

volumes:
  postgres_data:
```

---

## Backup & Recovery

### Database Backups

**PostgreSQL Automated Backups:**
```bash
# Daily backup cron job
0 2 * * * pg_dump -U user geo_api | gzip > /backups/geo_api_$(date +\%Y\%m\%d).sql.gz

# Retention: Keep last 30 days
find /backups -name "geo_api_*.sql.gz" -mtime +30 -delete
```

### File Storage Backups

**S3 Versioning:** Enable versioning on S3 bucket
**Backup Script:**
```bash
#!/bin/bash
aws s3 sync /var/app/uploads s3://backup-bucket/uploads/$(date +%Y%m%d)/
```

### Disaster Recovery

1. **RTO (Recovery Time Objective)**: < 1 hour
2. **RPO (Recovery Point Objective)**: < 24 hours
3. **Backup Testing**: Monthly restore test
4. **Multi-Region**: Deploy to multiple regions for HA

---

## Production Deployment Checklist

### Pre-Launch
- [ ] Load testing completed (siege, locust, k6)
- [ ] Security audit performed
- [ ] Penetration testing done
- [ ] SSL certificates installed
- [ ] Database backups configured
- [ ] Monitoring dashboards created
- [ ] Runbook documentation written
- [ ] On-call rotation established

### Launch Day
- [ ] DNS records updated
- [ ] SSL verification
- [ ] Health checks passing
- [ ] Monitoring alerts configured
- [ ] Log aggregation working
- [ ] Backup verification

### Post-Launch
- [ ] Monitor error rates
- [ ] Track response times
- [ ] Review logs daily
- [ ] Update documentation
- [ ] Plan capacity scaling
- [ ] Schedule regular security reviews

---

## Troubleshooting

### Common Issues

**Issue: High memory usage**
- Solution: Implement file streaming, limit concurrent uploads

**Issue: Slow parsing for large files**
- Solution: Move to background processing with Celery

**Issue: Database connection timeouts**
- Solution: Increase pool size, enable connection pooling

**Issue: Upload failures**
- Solution: Check disk space, verify permissions, review logs

---

## Support & Maintenance

### Regular Maintenance Tasks

- **Daily**: Monitor logs, check error rates
- **Weekly**: Review performance metrics, database growth
- **Monthly**: Security updates, dependency updates
- **Quarterly**: Load testing, disaster recovery drill

### Version Updates

```bash
# Check for security vulnerabilities
pip install safety
safety check

# Update dependencies
pip list --outdated
pip install -U package_name

# Run tests after updates
pytest -v
```

---

## Additional Resources

- [FastAPI Best Practices](https://fastapi.tiangolo.com/deployment/)
- [SQLAlchemy Performance Tips](https://docs.sqlalchemy.org/en/14/faq/performance.html)
- [GeoPandas Optimization](https://geopandas.org/en/stable/docs/user_guide/io.html#performance-considerations)
- [OWASP API Security](https://owasp.org/www-project-api-security/)

---

**Last Updated:** 2024-10-07  
**Version:** 1.0.0
