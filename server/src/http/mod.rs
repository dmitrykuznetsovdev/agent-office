//! HTTP: роутер. Эндпойнты добавляют пакеты эпика, граница безопасности — пакет S2.

use std::sync::Arc;

use axum::Router;

use crate::state::AppState;

pub fn router(state: Arc<AppState>) -> Router {
    Router::new().with_state(state)
}
