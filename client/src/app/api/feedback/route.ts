import { NextRequest, NextResponse } from 'next/server';
import { auth } from '@/auth';
import { readFeedback, writeFeedback, saveImage, StoredFeedback } from '@/lib/feedback-storage';

//Interface for feedback data received from the frontend form
interface FeedbackData {
  description: string;
  userAgent: string;
  url: string;
  userId?: string;
  userEmail?: string;
}

/**
 * Submits new feedback from authenticated users
 * 
 * Security: Requires authentication (any authenticated user can submit)
 * 
 * Request: FormData containing:
 *   - feedbackData: JSON string with feedback details
 *   - image_0, image_1, image_2: Optional image files (max 3)
 *
 * Usage: Called when user clicks "Send" on feedback form
 */
export async function POST(req: NextRequest) {
  try {
    const session = await auth();
    if (!session?.userId) return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });

    // Parse form data from request
    const formData = await req.formData();
    const feedbackDataStr = formData.get('feedbackData') as string;
    
    // Validate required feedback data exists
    if (!feedbackDataStr) return NextResponse.json({ error: 'Missing feedback data' }, { status: 400 });

    // Parse and validate feedback content
    const feedbackData: FeedbackData = JSON.parse(feedbackDataStr);
    if (!feedbackData.description?.trim()) {
      return NextResponse.json({ error: 'Feedback description is required' }, { status: 400 });
    }

    // Generate unique ID for this feedback
    const feedbackId = `feedback_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;

    // Process any uploaded images (max 3)
    const images = [];
    const entries = Array.from(formData.entries());
    
    for (const [key, value] of entries) {
      // Look for image fields (image_0, image_1, image_2)
      if (key.startsWith('image_') && value && typeof value === 'object' && 'arrayBuffer' in value) {
        const imageData = await saveImage(value as File, feedbackId);
        images.push(imageData);
      }
    }

    // Create complete feedback object
    const newFeedback: StoredFeedback = {
      id: feedbackId,
      description: feedbackData.description.trim(),
      userAgent: feedbackData.userAgent,
      url: feedbackData.url,
      userId: session.userId,
      userEmail: session.user?.email || '',
      timestamp: new Date().toISOString(),
      images: images,
      status: "new" // All new feedback starts with "new" status
    };

    // Save to storage
    const existingFeedback = await readFeedback();
    existingFeedback.push(newFeedback);
    await writeFeedback(existingFeedback);

    // Log successful submission for monitoring
    // console.log('Feedback received and saved:', {
    //   id: newFeedback.id,
    //   description: newFeedback.description.substring(0, 100) + '...',
    //   userEmail: newFeedback.userEmail,
    //   imagesCount: images.length,
    //   totalImageSize: images.reduce((sum, img) => sum + img.size, 0),
    // });

    return NextResponse.json({ 
      success: true, 
      message: 'Feedback submitted successfully',
      feedbackId: newFeedback.id 
    });
  } catch (error) {
    console.error('Error processing feedback:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}