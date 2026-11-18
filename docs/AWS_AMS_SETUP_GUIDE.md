# Amazon Marketing Stream (AMS) AWS Setup Guide

This guide details the exact AWS infrastructure and permissions required to receive real-time hourly Sponsored Products performance metrics (`sp-traffic` and `sp-conversion`) from Amazon Marketing Stream.

---

## 1. Prerequisites
- An active AWS Account.
- Your Amazon Ads Login with Amazon (LWA) Client ID and Advertiser Profile ID (for India `IN`, marketplace endpoint is `https://advertising-api-eu.amazon.com`).
- AWS Region recommendation: `eu-west-1` (Ireland) to match the European/India Amazon Ads endpoint.

---

## 2. Step 1: Create Amazon SQS Queue
1. Log in to the **AWS Management Console** and open **Amazon SQS**.
2. Click **Create queue**.
3. Configuration:
   - **Type:** Standard (FIFO queues are **not** supported by Amazon Marketing Stream).
   - **Name:** `adsmanager-ams-stream-queue` (or your preferred name).
   - **Visibility timeout:** 300 seconds (5 minutes).
   - **Message retention period:** 4 days (or up to 14 days).
   - **Server-side encryption:** Enabled (Amazon SQS-managed key `SSE-SQS`).
4. Copy your queue's **ARN** (e.g. `arn:aws:sqs:eu-west-1:123456789012:adsmanager-ams-stream-queue`) and **URL**.

---

## 3. Step 2: Configure SQS Access Policy
To allow Amazon Marketing Stream to publish messages into your queue, edit your SQS Queue **Access Policy** and insert the following policy:

```json
{
  "Version": "2012-10-17",
  "Id": "AMSQueuePolicy",
  "Statement": [
    {
      "Sid": "AllowAmazonMarketingStreamPublish",
      "Effect": "Allow",
      "Principal": {
        "Service": "marketingstream.amazonaws.com"
      },
      "Action": "sqs:SendMessage",
      "Resource": "arn:aws:sqs:eu-west-1:YOUR_ACCOUNT_ID:adsmanager-ams-stream-queue",
      "Condition": {
        "ArnEquals": {
          "aws:SourceArn": "arn:aws:sqs:eu-west-1:YOUR_ACCOUNT_ID:adsmanager-ams-stream-queue"
        }
      }
    }
  ]
}
```

*(Note: Depending on your Amazon Ads region, Amazon's service principal is either `marketingstream.amazonaws.com` or specific Amazon Ads AWS account principals specified in the official Amazon Advertising onboarding guide).*

---

## 4. Step 3: Create Stream Subscriptions via Amazon Ads API
Run the following cURL commands using your LWA credentials to subscribe to `sp-traffic` and `sp-conversion`:

### 4.1 Subscribe to `sp-traffic`
```bash
curl --location --request POST 'https://advertising-api-eu.amazon.com/streams/subscriptions' \
--header 'Amazon-Advertising-API-ClientId: YOUR_LWA_CLIENT_ID' \
--header 'Authorization: Bearer YOUR_ACCESS_TOKEN' \
--header 'Amazon-Advertising-API-Scope: YOUR_INDIA_PROFILE_ID' \
--header 'Content-Type: application/vnd.MarketingStreamSubscriptions.v1+json' \
--data '{
  "dataSetId": "sp-traffic",
  "destination": {
    "sqsDestination": {
      "queueArn": "arn:aws:sqs:eu-west-1:YOUR_ACCOUNT_ID:adsmanager-ams-stream-queue"
    }
  }
}'
```

### 4.2 Subscribe to `sp-conversion`
```bash
curl --location --request POST 'https://advertising-api-eu.amazon.com/streams/subscriptions' \
--header 'Amazon-Advertising-API-ClientId: YOUR_LWA_CLIENT_ID' \
--header 'Authorization: Bearer YOUR_ACCESS_TOKEN' \
--header 'Amazon-Advertising-API-Scope: YOUR_INDIA_PROFILE_ID' \
--header 'Content-Type: application/vnd.MarketingStreamSubscriptions.v1+json' \
--data '{
  "dataSetId": "sp-conversion",
  "destination": {
    "sqsDestination": {
      "queueArn": "arn:aws:sqs:eu-west-1:YOUR_ACCOUNT_ID:adsmanager-ams-stream-queue"
    }
  }
}'
```

---

## 5. Step 4: Configure Backend Environment Variables
In your backend deployment environment, set the following environment variable **NAMES**:

| Variable Name | Description | Example / Default |
|---|---|---|
| `AMS_CONSUMER_ENABLED` | Feature flag to start the hourly SQS consumer | `true` (default: `false`) |
| `AWS_REGION` | AWS Region where the SQS queue is hosted | `eu-west-1` |
| `AMS_SQS_QUEUE_URL` | Full URL of your Amazon SQS queue | `https://sqs.eu-west-1.amazonaws.com/...` |
| `AWS_ACCESS_KEY_ID` | IAM User / Role access key for polling SQS | Standard AWS credentials |
| `AWS_SECRET_ACCESS_KEY` | IAM User / Role secret key for polling SQS | Standard AWS credentials |

> **Security Note:** If `AMS_CONSUMER_ENABLED` is false or unset, the consumer remains idle and never contacts AWS.
