#![allow(dead_code)]

#[path = "../../../src/video/format.rs"]
pub mod format;
#[path = "../../../src/video/frame.rs"]
pub mod frame;
#[path = "../../../src/web/mjpeg.rs"]
mod http_delivery;
#[path = "../../../src/stream/mjpeg.rs"]
pub mod mjpeg_handler;

pub mod video {
    pub use crate::{format, frame};
    pub use format::PixelFormat;
    pub use frame::VideoFrame;
}

pub mod stream {
    pub use crate::mjpeg_handler as mjpeg;
}

// The application event envelope is irrelevant to these transport tests.
// Keep just the type used by the real handler's get_clients_stat().
pub mod events {
    pub mod types {
        pub struct ClientStats {
            pub id: String,
            pub fps: u32,
            pub connected_secs: u64,
        }
    }
}

#[cfg(test)]
mod transport_tests {
    use super::*;
    use axum::{routing::get, Router};
    use std::sync::Arc;
    use std::time::Duration;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[tokio::test]
    async fn http_socket_delivers_continuous_multipart_frames() {
        let handler = Arc::new(mjpeg_handler::MjpegStreamHandler::with_drop_limit(0));
        handler.set_online();
        let for_route = handler.clone();
        let app = Router::new().route(
            "/stream",
            get(move || {
                let handler = for_route.clone();
                async move { http_delivery::response(handler, "http-test".into()) }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        use axum::serve::ListenerExt;
        let server = tokio::spawn(async move {
            axum::serve(listener.tap_io(|tcp| tcp.set_nodelay(true).unwrap()), app)
                .await
                .unwrap();
        });
        let capture = handler.clone();
        let producer = tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_millis(5));
            for seq in 0..60 {
                interval.tick().await;
                let mut bytes = vec![seq as u8; 20_000];
                bytes[..2].copy_from_slice(&[0xff, 0xd8]);
                bytes[19_998..].copy_from_slice(&[0xff, 0xd9]);
                capture.update_frame(video::VideoFrame::from_vec(
                    bytes,
                    format::Resolution::HD720,
                    format::PixelFormat::Mjpeg,
                    0,
                    seq,
                ));
            }
            tokio::time::sleep(Duration::from_millis(30)).await;
            capture.set_offline();
        });

        let mut client = tokio::net::TcpStream::connect(addr).await.unwrap();
        client
            .write_all(b"GET /stream HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            .await
            .unwrap();
        let mut wire = Vec::new();
        tokio::time::timeout(Duration::from_secs(5), client.read_to_end(&mut wire))
            .await
            .unwrap()
            .unwrap();
        producer.await.unwrap();
        server.abort();
        assert!(wire.starts_with(b"HTTP/1.1 200 OK\r\n"));
        let parts = wire
            .windows(b"--frame\r\n".len())
            .filter(|w| *w == b"--frame\r\n")
            .count();
        assert!(
            parts >= 40,
            "expected continuous delivery, got {parts} parts"
        );
        assert_eq!(handler.client_count(), 0);
    }
}
