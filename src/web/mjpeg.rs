//! Pull the latest frame only when Hyper polls the response body.
//!
//! A separate producer plus a capacity-one channel can retain a queued frame
//! AND a frame waiting in send(). Both grow stale under socket backpressure.

use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::{body::Body, http::header, response::Response};
use bytes::{BufMut, Bytes, BytesMut};

use crate::stream::mjpeg::{ClientGuard, MjpegStreamHandler};

pub(super) fn response(handler: Arc<MjpegStreamHandler>, client_id: String) -> Response {
    // Subscribe before taking the initial snapshot so no wakeup can be missed.
    let mut notify_rx = handler.subscribe();
    let guard = ClientGuard::new(client_id, handler.clone());
    let body_stream = async_stream::stream! {
        // Some drivers reuse/reset sequence numbers. capture_ts distinguishes
        // new frames even in that case, without resending a cached frame.
        let mut last_frame = None;
        let mut timeout_count = 0;
        let mut resend = false;
        let mut report_at = Instant::now();
        let mut report_frames = 0u64;
        let mut max_frame_age_ms = 0u64;
        let mut max_body_poll_gap_ms = 0u64;

        loop {
            if !handler.is_online() {
                break;
            }
            if let Some(frame) = handler.current_frame() {
                let identity = (frame.sequence, frame.capture_ts);
                if (resend || last_frame != Some(identity)) && frame.is_valid_jpeg() {
                    resend = false;
                    last_frame = Some(identity);
                    max_frame_age_ms = max_frame_age_ms.max(frame.age().as_millis() as u64);
                    // Keep one complete multipart part per body item. Splitting
                    // headers/JPEG/trailer adds small writes and scheduling work.
                    let data = create_mjpeg_part(frame.data());
                    // Don't retain the capture buffer while the socket stalls.
                    drop(frame);
                    handler.record_http_body_frame(guard.id());
                    report_frames += 1;
                    let elapsed = report_at.elapsed().as_secs_f64();
                    if elapsed >= 5.0 {
                        tracing::debug!(
                            client_id = %guard.id(),
                            body_fps = report_frames as f64 / elapsed,
                            max_frame_age_ms,
                            max_body_poll_gap_ms,
                            "MJPEG delivery (body polling, not browser display)"
                        );
                        report_at = Instant::now();
                        report_frames = 0;
                        max_frame_age_ms = 0;
                        max_body_poll_gap_ms = 0;
                    }
                    let yielded_at = Instant::now();
                    yield Ok::<Bytes, std::io::Error>(data);
                    max_body_poll_gap_ms = max_body_poll_gap_ms
                        .max(yielded_at.elapsed().as_millis() as u64);
                    // On the next poll, take a fresh snapshot before waiting.
                    continue;
                }
            }
            resend = false;

            match tokio::time::timeout(Duration::from_secs(5), notify_rx.recv()).await {
                Ok(Ok(())) | Ok(Err(tokio::sync::broadcast::error::RecvError::Lagged(_))) => {
                    timeout_count = 0;
                }
                Ok(Err(tokio::sync::broadcast::error::RecvError::Closed)) => break,
                Err(_) => {
                    timeout_count += 1;
                    if timeout_count > 6 {
                        break;
                    }
                    resend = true; // Retain the existing idle keepalive policy.
                }
            }
        }
        // The body owns guard; dropping a response immediately unregisters it,
        // even if it has never been polled. There is no orphan producer task.
        drop(guard);
    };

    Response::builder()
        .header(
            header::CONTENT_TYPE,
            "multipart/x-mixed-replace; boundary=frame",
        )
        .header(header::CACHE_CONTROL, "no-cache, no-store, must-revalidate")
        .header(header::PRAGMA, "no-cache")
        .header(header::EXPIRES, "0")
        .header("X-Accel-Buffering", "no")
        .header("X-One-KVM-Mjpeg", "latest-frame-v1")
        .body(Body::from_stream(body_stream))
        .unwrap()
}

fn create_mjpeg_part(jpeg_data: &[u8]) -> Bytes {
    let mut buf = BytesMut::with_capacity(128 + jpeg_data.len());
    buf.put_slice(b"--frame\r\nContent-Type: image/jpeg\r\n");
    buf.put_slice(format!("Content-Length: {}\r\n\r\n", jpeg_data.len()).as_bytes());
    buf.put_slice(jpeg_data);
    buf.put_slice(b"\r\n");
    buf.freeze()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::video::{format::Resolution, PixelFormat, VideoFrame};
    use futures::StreamExt;

    fn frame(sequence: u64, value: u8) -> VideoFrame {
        let mut data = vec![value; 256];
        data[..2].copy_from_slice(&[0xff, 0xd8]);
        data[254..].copy_from_slice(&[0xff, 0xd9]);
        VideoFrame::from_vec(data, Resolution::VGA, PixelFormat::Mjpeg, 0, sequence)
    }

    #[tokio::test]
    async fn slow_consumer_gets_latest_frame_and_reused_sequence() {
        let handler = Arc::new(MjpegStreamHandler::with_drop_limit(0));
        handler.update_frame(frame(0, 1));
        let response = response(handler.clone(), "slow".into());
        assert_eq!(response.headers()["x-one-kvm-mjpeg"], "latest-frame-v1");
        let mut body = response.into_body().into_data_stream();
        let first = body.next().await.unwrap().unwrap();
        assert!(first.windows(32).any(|s| s == [1; 32]));

        // No body polling while the capture side publishes many new frames.
        for value in 2..=60 {
            handler.update_frame(frame(0, value));
        }
        let next = body.next().await.unwrap().unwrap();
        assert!(next.windows(32).any(|s| s == [60; 32]));
        assert_eq!(handler.diagnostics().http_body_frames, 2);
        drop(body);
        assert_eq!(handler.client_count(), 0);
    }

    #[tokio::test]
    async fn offline_wakes_pending_body() {
        let handler = Arc::new(MjpegStreamHandler::with_drop_limit(0));
        handler.update_frame(frame(1, 1));
        let mut body = response(handler.clone(), "offline".into())
            .into_body()
            .into_data_stream();
        body.next().await.unwrap().unwrap();
        // Drain pending notifications; set_offline must wake the idle body.
        let stop = async {
            tokio::task::yield_now().await;
            handler.set_offline();
        };
        let (next, ()) = tokio::join!(
            tokio::time::timeout(Duration::from_secs(1), body.next()),
            stop
        );
        assert!(next.unwrap().is_none());
        assert_eq!(handler.client_count(), 0);
    }

    #[test]
    fn unpolled_body_does_not_leak_client() {
        let handler = Arc::new(MjpegStreamHandler::new());
        let response = response(handler.clone(), "unpolled".into());
        assert_eq!(handler.client_count(), 1);
        drop(response);
        assert_eq!(handler.client_count(), 0);
    }

    #[test]
    fn multipart_length_and_delimiters_are_exact() {
        assert_eq!(
            create_mjpeg_part(b"jpeg").as_ref(),
            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\njpeg\r\n"
        );
    }
}
