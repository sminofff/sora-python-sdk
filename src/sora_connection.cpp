#include "sora_connection.h"

#include <chrono>

// Boost
#include <boost/asio/signal_set.hpp>

// nonobind
#include <nanobind/nanobind.h>

namespace nb = nanobind;

SoraConnection::SoraConnection(DisposePublisher* publisher)
    : publisher_(publisher) {
  publisher_->AddSubscriber(this);
}

SoraConnection::~SoraConnection() {
  if (publisher_) {
    publisher_->RemoveSubscriber(this);
  }
  Disposed();
}

void SoraConnection::Disposed() {
  DisposePublisher::Disposed();
  Disconnect();
  publisher_ = nullptr;
}

void SoraConnection::PubliserDisposed() {
  Disposed();
}

void SoraConnection::Init(sora::SoraSignalingConfig& config) {
  ioc_.reset(new boost::asio::io_context(1));
  config.io_context = ioc_.get();
  conn_ = sora::SoraSignaling::Create(config);
}

void SoraConnection::Connect() {
  conn_->Connect();

  thread_.reset(new std::thread([this]() { ioc_->run(); }));
}

void SoraConnection::Disconnect() {
  if (thread_) {
    // Disconnect の中で OnDisconnect が呼ばれるので GIL をリリースする
    nb::gil_scoped_release release;
    conn_->Disconnect();
    thread_->join();
    thread_ = nullptr;
  }
  // Connection から生成したものは、ここで消す
  audio_sender_ = nullptr;
  video_sender_ = nullptr;
  conn_ = nullptr;
}

void SoraConnection::SetAudioTrack(SoraTrackInterface* audio_source) {
  if (audio_sender_) {
    audio_sender_->SetTrack(audio_source->GetTrack().get());
  }
  if (audio_source_) {
    audio_source_->RemoveSubscriber(this);
  }
  audio_source->AddSubscriber(this);
  audio_source_ = audio_source;
}

void SoraConnection::SetVideoTrack(SoraTrackInterface* video_source) {
  if (video_sender_) {
    video_sender_->SetTrack(video_source->GetTrack().get());
  }
  if (video_source_) {
    video_source_->RemoveSubscriber(this);
  }
  video_source->AddSubscriber(this);
  video_source_ = video_source;
}

void SoraConnection::OnSetOffer(std::string offer) {
  std::string stream_id = rtc::CreateRandomString(16);
  if (audio_source_) {
    webrtc::RTCErrorOr<rtc::scoped_refptr<webrtc::RtpSenderInterface>>
        audio_result = conn_->GetPeerConnection()->AddTrack(
            audio_source_->GetTrack(), {stream_id});
    if (audio_result.ok()) {
      audio_sender_ = audio_result.value();
    }
  }
  if (video_source_) {
    webrtc::RTCErrorOr<rtc::scoped_refptr<webrtc::RtpSenderInterface>>
        video_result = conn_->GetPeerConnection()->AddTrack(
            video_source_->GetTrack(), {stream_id});
    if (video_result.ok()) {
      video_sender_ = video_result.value();
    }
  }
  if (on_set_offer_) {
    on_set_offer_(offer);
  }
}

void SoraConnection::OnDisconnect(sora::SoraSignalingErrorCode ec,
                                  std::string message) {
  ioc_->stop();
  if (on_disconnect_) {
    on_disconnect_(ec, message);
  }
}

void SoraConnection::OnNotify(std::string text) {
  if (on_set_offer_) {
    on_set_offer_(text);
  }
}

void SoraConnection::OnPush(std::string text) {
  if (on_push_) {
    on_push_(text);
  }
}

void SoraConnection::OnMessage(std::string label, std::string data) {
  if (on_message_) {
    on_message_(label, data);
  }
}

void SoraConnection::OnTrack(
    rtc::scoped_refptr<webrtc::RtpTransceiverInterface> transceiver) {
  if (on_track_) {
    // shared_ptr になってないのでリークする
    auto track = std::make_shared<SoraTrackInterface>(
        this, transceiver->receiver()->track());
    AddSubscriber(track.get());
    on_track_(track);
  }
}

void SoraConnection::OnRemoveTrack(
    rtc::scoped_refptr<webrtc::RtpReceiverInterface> receiver) {}

void SoraConnection::OnDataChannel(std::string label) {
  if (on_data_channel_) {
    on_data_channel_(label);
  }
}
