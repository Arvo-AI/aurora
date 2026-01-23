import { writeFile, readFile, mkdir } from 'fs/promises';
import { existsSync } from 'fs';
import path from 'path';

export interface StoredFeedback {
  id: string;
  description: string;
  userAgent: string;
  url: string;
  userId: string;
  userEmail: string;
  timestamp: string;
  images: { name: string; size: number; type: string; filename: string }[];
  status: "new" | "in-progress" | "resolved" | "closed";
}

// File system paths for feedback storage
const FEEDBACK_DIR = path.join(process.cwd(), 'feedback-data');
const FEEDBACK_FILE = path.join(FEEDBACK_DIR, 'feedback.json');
const IMAGES_DIR = path.join(FEEDBACK_DIR, 'images');

export async function ensureFeedbackDir() {
  if (!existsSync(FEEDBACK_DIR)) await mkdir(FEEDBACK_DIR, { recursive: true });
  if (!existsSync(IMAGES_DIR)) await mkdir(IMAGES_DIR, { recursive: true });
}


export async function readFeedback(): Promise<StoredFeedback[]> {
  try {
    await ensureFeedbackDir();
    if (!existsSync(FEEDBACK_FILE)) return [];
    const data = await readFile(FEEDBACK_FILE, 'utf-8');
    return JSON.parse(data);
  } catch (error) {
    console.error('Error reading feedback:', error);
    return [];
  }
}


export async function writeFeedback(feedback: StoredFeedback[]) {
  try {
    await ensureFeedbackDir();
    await writeFile(FEEDBACK_FILE, JSON.stringify(feedback, null, 2));
  } catch (error) {
    console.error('Error writing feedback:', error);
  }
}


export async function saveImage(file: File, feedbackId: string): Promise<{ name: string; size: number; type: string; filename: string }> {
  const arrayBuffer = await file.arrayBuffer();
  const buffer = Buffer.from(arrayBuffer);
  
  // Generate unique filename to prevent conflicts
  const fileExtension = file.name.split('.').pop() || 'jpg';
  const filename = `${feedbackId}_${Date.now()}_${Math.random().toString(36).substr(2, 6)}.${fileExtension}`;
  const filepath = path.join(IMAGES_DIR, filename);
  
  // Save the image file to disk
  await writeFile(filepath, buffer);
  
  return {
    name: file.name,        // Original filename from user
    size: file.size,        // File size in bytes
    type: file.type,        // MIME type (e.g., 'image/jpeg')
    filename: filename,     // Generated unique filename on disk
  };
}


export function getImagePath(filename: string): string {
  return path.join(IMAGES_DIR, filename);
}