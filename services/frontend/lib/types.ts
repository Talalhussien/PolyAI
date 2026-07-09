export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  image_base64?: string;
  annotated_image?: string;
  processed_image_s3_key?: string;
}
