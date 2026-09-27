package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/dongmaoz/buda/authd/internal/authd"
	"github.com/dongmaoz/buda/authd/migrations"
	_ "github.com/mattn/go-sqlite3"
	"github.com/pressly/goose/v3"
	"go.uber.org/zap"
)

func main() {
	log, _ := zap.NewProduction()
	defer log.Sync() //nolint:errcheck

	cfg, err := authd.LoadConfig()
	if err != nil {
		log.Fatal("invalid configuration", zap.Error(err))
	}
	if err := os.MkdirAll(filepath.Dir(cfg.DBPath), 0o750); err != nil {
		log.Fatal("create database directory", zap.Error(err))
	}
	dsn := fmt.Sprintf("file:%s?_journal_mode=WAL&_busy_timeout=5000&_foreign_keys=on&_synchronous=NORMAL", cfg.DBPath)
	conn, err := sql.Open("sqlite3", dsn)
	if err != nil {
		log.Fatal("open database", zap.Error(err))
	}
	defer conn.Close()
	conn.SetMaxOpenConns(4)
	conn.SetMaxIdleConns(4)
	goose.SetBaseFS(migrations.Files)
	if err := goose.SetDialect("sqlite3"); err != nil {
		log.Fatal("configure migrations", zap.Error(err))
	}
	if err := goose.Up(conn, "."); err != nil {
		log.Fatal("apply migrations", zap.Error(err))
	}

	tokens, err := authd.LoadTokenManager(cfg.PrivateKeyFile, cfg.KeyID, cfg.TenantKey)
	if err != nil {
		log.Fatal("load signing key", zap.Error(err))
	}
	if err := tokens.LoadPreviousJWKS(os.Getenv("JWT_PREVIOUS_JWKS_FILE")); err != nil {
		log.Fatal("load previous public keys", zap.Error(err))
	}
	store := authd.NewSQLStore(conn)
	provider := authd.NewFeishuProvider(cfg)
	handler := authd.NewServer(cfg, store, provider, tokens, log).Router()
	httpServer := &http.Server{
		Addr: cfg.ListenAddr, Handler: handler,
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second,
		WriteTimeout: 15 * time.Second, IdleTimeout: 60 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		log.Info("authd listening", zap.String("address", cfg.ListenAddr))
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatal("serve", zap.Error(err))
		}
	}()
	go func() {
		ticker := time.NewTicker(time.Minute)
		defer ticker.Stop()
		for {
			select {
			case now := <-ticker.C:
				if err := store.DeleteExpired(context.Background(), now); err != nil {
					log.Warn("delete expired authentication records", zap.Error(err))
				}
			case <-ctx.Done():
				return
			}
		}
	}()
	<-ctx.Done()
	shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := httpServer.Shutdown(shutdown); err != nil {
		log.Error("shutdown", zap.Error(err))
	}
}
