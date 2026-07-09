#!/bin/sh

if [ "$ENABLE_DB_BACKUP" != "true" ]; then
  echo "Backup desabilitado."
  tail -f /dev/null
fi

while true; do
  TIMESTAMP=$(date +%Y%m%d_%H%M%S)
  PGPASSWORD=$POSTGRES_PASSWORD pg_dump -h medessence_db -U $POSTGRES_USER $POSTGRES_DB | gzip > /backups/backup_$TIMESTAMP.sql.gz
  echo "Backup criado: backup_$TIMESTAMP.sql.gz"
  find /backups -name "*.sql.gz" -mtime +7 -delete
  sleep 86400
done
